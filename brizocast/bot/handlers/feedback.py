"""👍/👎 feedback callback handler (thin adapter, Req 12.3, 12.4).

A single thin Telegram adapter, assembled by :func:`build_feedback_handlers`:
when a user taps a thumbs-up/thumbs-down button on an alert, the
:class:`~telegram.ext.CallbackQueryHandler` here decodes the alert identity from
the button's ``callback_data`` and persists the rating.

The handler is registered against the feedback callback-data scheme owned by
:mod:`brizocast.bot.keyboards.feedback`: it matches the ``fb:<version>:`` prefix
(see :data:`FEEDBACK_CALLBACK_PATTERN`), confirms the payload with
:func:`~brizocast.bot.keyboards.feedback.is_feedback_callback`, and decodes it
with :func:`~brizocast.bot.keyboards.feedback.parse_feedback_callback`. The
decoded ``subscription_id``, ``spot_key``, ``surf_score``, and ``rating`` are
passed verbatim to
:meth:`~brizocast.services.feedback_service.FeedbackService.record_feedback`,
which persists a ``Feedback`` row (Req 12.4). The tap is acknowledged with a
brief toast so Telegram stops the button's loading spinner (Req 12.3).

Thin handler + dependency injection
-----------------------------------
:func:`build_feedback_handlers` is a builder closure: it receives the live
:class:`~brizocast.services.feedback_service.FeedbackService` and binds it into
the callback, returning the handler for the composition root (task 11.1) to
register on the ``Application``. The handler only parses the callback, calls the
service, and answers the query — no persistence or scoring logic lives here.

Requirements covered: 12.3, 12.4.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import BaseHandler, CallbackQueryHandler, ContextTypes

from brizocast.bot.keyboards.feedback import (
    CALLBACK_PREFIX,
    CALLBACK_VERSION,
    is_feedback_callback,
    parse_feedback_callback,
)
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.services.feedback_service import FeedbackService

__all__ = ["FEEDBACK_CALLBACK_PATTERN", "build_feedback_handlers"]

# Routes only current-version feedback callbacks to this handler; mirrors the
# prefix/version checked by ``is_feedback_callback``.
FEEDBACK_CALLBACK_PATTERN = rf"^{CALLBACK_PREFIX}:{CALLBACK_VERSION}:"

# Toast shown when a tap is recorded / ignored (Req 12.3).
_THANKS_TEXT = "Thanks for the feedback!"
_IGNORED_TEXT = "Sorry, that feedback button has expired."

_Handler = BaseHandler[Update, ContextTypes.DEFAULT_TYPE, object]


def build_feedback_handlers(
    feedback_service: FeedbackService,
    *,
    logger: BoundLogger | None = None,
) -> list[_Handler]:
    """Build the 👍/👎 feedback callback handler bound to ``feedback_service``.

    The injected :class:`FeedbackService` is captured by closure, so the handler
    persists feedback (Req 12.4) without constructing services or touching the
    ``Application``.

    :param feedback_service: Service used to persist the user's rating.
    :param logger: Optional bound logger; one is created when omitted.
    :returns: A single-element list holding the feedback
        :class:`~telegram.ext.CallbackQueryHandler`.
    """

    log = logger or get_logger(__name__)

    async def on_feedback(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Decode the tapped feedback button and persist the rating (Req 12.4)."""

        query = update.callback_query
        if query is None or query.data is None:
            return

        raw = query.data
        if not is_feedback_callback(raw):
            # Pattern-routed here but not a current-version feedback payload;
            # acknowledge so the spinner stops and ignore it.
            await query.answer(_IGNORED_TEXT)
            return

        try:
            data = parse_feedback_callback(raw)
        except ValueError:
            log.warning("ignoring malformed feedback callback: %r", raw)
            await query.answer(_IGNORED_TEXT)
            return

        await feedback_service.record_feedback(
            data.subscription_id,
            data.spot_key,
            data.surf_score,
            data.rating,
        )
        # Acknowledge the tap with a brief toast (Req 12.3).
        await query.answer(_THANKS_TEXT)

    return [CallbackQueryHandler(on_feedback, pattern=FEEDBACK_CALLBACK_PATTERN)]
