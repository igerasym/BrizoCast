"""Message delivery port, Telegram adapter, and resilient retry/batch dispatch.

This module is the **only** place in the notification stack that knows about
``python-telegram-bot``. The notification engine and scheduler depend on the
framework-neutral :class:`MessageSender` port (and the
:class:`RetryingNotificationSender` built around it), never on ``telegram``
directly, keeping Telegram swappable and the engine unit-testable with a fake
sender.

Responsibilities
----------------
* :class:`MessageSender` — the delivery port: send one chat message with text
  and an optional inline keyboard. Implementations raise on a delivery failure.
* :class:`TelegramSender` — the port implementation wrapping a
  :class:`telegram.Bot`. It translates the neutral
  :class:`InlineButton` keyboard into Telegram's
  ``InlineKeyboardMarkup``/``InlineKeyboardButton`` and sends the message.
* :class:`RetryingNotificationSender` — wraps any :class:`MessageSender` and
  adds the delivery policy required by the design:

  - **Retry** each message up to ``NOTIFY_RETRY_COUNT`` attempts; on exhaustion
    it returns a *failed* :class:`SendResult` so the caller can add the alert to
    the subscription's next digest rather than dropping it (Req 10.4).
  - **Resilience** — :meth:`send_batch` attempts *every* request and returns a
    per-item success/failure list; a failure for one user/notification logs and
    continues instead of aborting the batch (Req 18.3, Property 26). Each
    delivery failure is logged with context.

The retry/batch layer catches broad :class:`Exception` from the underlying
sender (network errors, Telegram API errors, …) so a single bad delivery never
propagates out of a batch. Cancellation and other :class:`BaseException`s are
deliberately **not** swallowed.

Requirements covered: 10.4, 18.3 (supports Property 26). Feedback persistence
(Req 12.4, 12.5) lives in :mod:`brizocast.services.feedback_service`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from brizocast.core.logging import BoundLogger, get_logger

__all__ = [
    "InlineButton",
    "InlineKeyboard",
    "MessageSender",
    "TelegramSender",
    "SendRequest",
    "SendResult",
    "RetryingNotificationSender",
]


@dataclass(frozen=True)
class InlineButton:
    """A framework-neutral inline-keyboard button.

    Carries the visible ``text`` and the ``callback_data`` payload echoed back
    when the user taps the button (used by the 👍/👎 feedback controls).
    """

    text: str
    callback_data: str


# A neutral inline keyboard: rows of buttons. Adapters (e.g. ``TelegramSender``)
# translate this into their framework's native keyboard type.
InlineKeyboard = Sequence[Sequence[InlineButton]]


@runtime_checkable
class MessageSender(Protocol):
    """Port for delivering a single chat message (Req 10.3, 10.4).

    Implementations send ``text`` (with an optional inline ``keyboard``) to the
    chat identified by ``chat_id`` and **raise** on a delivery failure so the
    caller's retry policy can react. The notification engine depends on this
    abstraction rather than on any concrete messaging backend.
    """

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
        photo_bytes: bytes | None = None,
    ) -> None:
        """Send ``text`` to ``chat_id``; raise on failure."""
        ...


class TelegramSender:
    """:class:`MessageSender` backed by a :class:`telegram.Bot`.

    Isolates the ``python-telegram-bot`` dependency: it converts the neutral
    :class:`InlineButton` keyboard into Telegram's native markup and delegates
    to :meth:`telegram.Bot.send_message`. Any error raised by the Bot
    propagates to the caller (the retry layer handles it).
    """

    def __init__(self, bot: Bot, *, logger: BoundLogger | None = None) -> None:
        """Initialise the sender.

        Args:
            bot: The ``python-telegram-bot`` ``Bot`` used for delivery.
            logger: Optional bound logger; one is created when omitted.
        """
        self._bot = bot
        self._log = logger or get_logger(__name__)

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
        photo_bytes: bytes | None = None,
    ) -> None:
        """Send ``text`` to ``chat_id`` with an optional inline keyboard.

        When ``photo_bytes`` is provided the message is sent as a photo caption
        (``send_photo``) rather than plain text, so the map image and the alert
        are delivered in one message.

        Raises whatever the underlying Bot raises on a delivery failure.
        """
        markup = self._to_markup(keyboard)
        if photo_bytes:
            import io
            await self._bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(photo_bytes),
                caption=text,
                parse_mode="Markdown",
                reply_markup=markup,
            )
        else:
            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=markup,
            )

    @staticmethod
    def _to_markup(
        keyboard: InlineKeyboard | None,
    ) -> InlineKeyboardMarkup | None:
        """Translate a neutral keyboard into Telegram's ``InlineKeyboardMarkup``."""
        if keyboard is None:
            return None
        rows = [
            [
                InlineKeyboardButton(
                    text=button.text, callback_data=button.callback_data
                )
                for button in row
            ]
            for row in keyboard
        ]
        return InlineKeyboardMarkup(rows)


@dataclass(frozen=True)
class SendRequest:
    """One message to deliver as part of a batch.

    ``ref`` is an opaque correlation handle the caller can use to route a
    failed delivery into the subscription's next digest (Req 10.4).
    ``photo_bytes`` is an optional PNG image to send as photo caption.
    """

    chat_id: int
    text: str
    keyboard: InlineKeyboard | None = None
    photo_bytes: bytes | None = None
    ref: object | None = None


@dataclass(frozen=True)
class SendResult:
    """Outcome of attempting to deliver a single :class:`SendRequest`.

    Attributes:
        request: The request this result corresponds to.
        delivered: ``True`` if the message was delivered within the allowed
            attempts; ``False`` if every attempt failed (the caller should fall
            back to the next digest — Req 10.4).
        attempts: Number of delivery attempts actually made.
        error: A string rendering of the last failure when ``delivered`` is
            ``False``; ``None`` on success.
    """

    request: SendRequest
    delivered: bool
    attempts: int
    error: str | None = None


class RetryingNotificationSender:
    """Adds retry-with-digest-fallback and resilient batching to a sender.

    Wraps any :class:`MessageSender`. Per the design's notification-delivery
    policy it retries each message up to ``NOTIFY_RETRY_COUNT`` attempts and,
    on exhaustion, returns a failed :class:`SendResult` (the engine adds the
    alert to the next digest — Req 10.4). :meth:`send_batch` attempts every
    request and returns per-item outcomes, logging and continuing past failures
    rather than aborting the batch (Req 18.3, Property 26).
    """

    def __init__(
        self,
        sender: MessageSender,
        *,
        retry_count: int,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the resilient sender.

        Args:
            sender: The underlying delivery port (e.g. :class:`TelegramSender`).
            retry_count: Maximum delivery attempts per message
                (``Settings.NOTIFY_RETRY_COUNT``). Values ``<= 0`` are treated
                as a single attempt so a message is always tried at least once.
            logger: Optional bound logger; one is created when omitted.
        """
        self._sender = sender
        self._retry_count = retry_count
        self._log = logger or get_logger(__name__)

    @property
    def max_attempts(self) -> int:
        """Number of attempts made per message (at least one)."""
        return self._retry_count if self._retry_count > 0 else 1

    async def send_with_retry(self, request: SendRequest) -> SendResult:
        """Attempt to deliver ``request``, retrying up to :attr:`max_attempts`.

        Returns a delivered :class:`SendResult` on success, or, if every attempt
        fails, a failed result the caller can route to the next digest (Req
        10.4). Never raises for a delivery failure; each failed attempt is
        logged with chat context (Req 18.3).
        """
        log = self._log.bind(chat_id=request.chat_id)
        last_error: str | None = None
        attempt = 0
        while attempt < self.max_attempts:
            attempt += 1
            try:
                await self._sender.send(
                    request.chat_id,
                    request.text,
                    keyboard=request.keyboard,
                    photo_bytes=request.photo_bytes,
                )
                return SendResult(request=request, delivered=True, attempts=attempt)
            except Exception as exc:  # noqa: BLE001 - one bad send must not abort.
                last_error = repr(exc)
                log.warning(
                    "notification delivery attempt %d/%d failed: %s",
                    attempt,
                    self.max_attempts,
                    exc,
                )

        log.error(
            "notification delivery failed after %d attempt(s); "
            "deferring to next digest",
            attempt,
        )
        return SendResult(
            request=request,
            delivered=False,
            attempts=attempt,
            error=last_error,
        )

    async def send_batch(self, requests: Iterable[SendRequest]) -> list[SendResult]:
        """Attempt every request in ``requests`` and return per-item outcomes.

        A failure for one request is logged and recorded as a failed
        :class:`SendResult`; the batch continues with the remaining requests
        rather than aborting (Req 18.3, Property 26). The returned list preserves
        input order, so callers can correlate each outcome with its request and
        add the failed ones to the appropriate digest (Req 10.4).
        """
        results: list[SendResult] = []
        for request in requests:
            results.append(await self.send_with_retry(request))
        delivered = sum(1 for result in results if result.delivered)
        self._log.info(
            "notification batch complete: %d/%d delivered",
            delivered,
            len(results),
        )
        return results
