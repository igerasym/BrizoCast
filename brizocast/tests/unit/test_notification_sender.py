"""Unit tests for the notification delivery layer (Req 10.4, 18.3).

Covers:

* retry-then-fail: a message that fails every attempt is retried exactly
  ``NOTIFY_RETRY_COUNT`` times and reported as undelivered so the caller can add
  it to the next digest (Req 10.4);
* retry-then-succeed: delivery stops as soon as an attempt succeeds;
* batch resilience: a batch with some failing deliveries still attempts every
  request and returns a per-item success/failure list (Req 18.3, Property 26);
* ``TelegramSender`` keyboard translation and Bot delegation, with a fake Bot
  (no real network/telegram calls).
"""

from __future__ import annotations

from typing import cast

import pytest
from telegram import Bot, InlineKeyboardMarkup

from brizocast.notifications.sender import (
    InlineButton,
    InlineKeyboard,
    RetryingNotificationSender,
    SendRequest,
    TelegramSender,
)


class _FlakySender:
    """Fake :class:`MessageSender` failing the first ``fail_times`` attempts."""

    def __init__(self, fail_times: int) -> None:
        self._fail_times = fail_times
        self.calls = 0

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
    ) -> None:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError(f"boom #{self.calls}")


class _ChatSelectiveSender:
    """Fake sender that always fails for a specific set of chat ids."""

    def __init__(self, failing_chat_ids: set[int]) -> None:
        self._failing = failing_chat_ids
        self.attempted_chat_ids: list[int] = []

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
    ) -> None:
        self.attempted_chat_ids.append(chat_id)
        if chat_id in self._failing:
            raise RuntimeError(f"delivery failed for {chat_id}")


@pytest.mark.asyncio
async def test_retry_exhaustion_signals_digest_fallback() -> None:
    """Every attempt fails -> undelivered after NOTIFY_RETRY_COUNT attempts."""
    retry_count = 3
    sender = _FlakySender(fail_times=retry_count)
    resilient = RetryingNotificationSender(sender, retry_count=retry_count)

    result = await resilient.send_with_retry(SendRequest(chat_id=1, text="surf's up"))

    assert result.delivered is False
    assert result.attempts == retry_count
    assert sender.calls == retry_count
    assert result.error is not None  # the last failure is captured


@pytest.mark.asyncio
async def test_retry_succeeds_before_exhaustion() -> None:
    """Delivery stops at the first successful attempt."""
    sender = _FlakySender(fail_times=2)
    resilient = RetryingNotificationSender(sender, retry_count=5)

    result = await resilient.send_with_retry(SendRequest(chat_id=7, text="go"))

    assert result.delivered is True
    assert result.attempts == 3  # 2 failures + 1 success
    assert sender.calls == 3


@pytest.mark.asyncio
async def test_zero_retry_count_attempts_once() -> None:
    """A retry count <= 0 still tries to deliver exactly once."""
    sender = _FlakySender(fail_times=0)
    resilient = RetryingNotificationSender(sender, retry_count=0)

    result = await resilient.send_with_retry(SendRequest(chat_id=2, text="hi"))

    assert result.delivered is True
    assert result.attempts == 1
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_batch_attempts_all_despite_failures() -> None:
    """A failure for one delivery never aborts the batch (Req 18.3, Property 26)."""
    sender = _ChatSelectiveSender(failing_chat_ids={2, 4})
    resilient = RetryingNotificationSender(sender, retry_count=2)

    requests = [SendRequest(chat_id=chat_id, text="msg") for chat_id in (1, 2, 3, 4, 5)]
    results = await resilient.send_batch(requests)

    # Every request produced a result, in input order.
    assert [r.request.chat_id for r in results] == [1, 2, 3, 4, 5]
    delivered = {r.request.chat_id: r.delivered for r in results}
    assert delivered == {1: True, 2: False, 3: True, 4: False, 5: True}

    # Every successful chat attempted once; every failing chat retried fully.
    assert sender.attempted_chat_ids == [1, 2, 2, 3, 4, 4, 5]

    # Failed items carry their ref so the caller can route them to a digest.
    failed = [r.request.chat_id for r in results if not r.delivered]
    assert failed == [2, 4]


@pytest.mark.asyncio
async def test_telegram_sender_sends_plain_message() -> None:
    """TelegramSender delegates to Bot.send_message with no markup when no keyboard."""
    captured: dict[str, object] = {}

    class _FakeBot:
        async def send_message(
            self, *, chat_id: int, text: str, reply_markup: object = None
        ) -> None:
            captured["chat_id"] = chat_id
            captured["text"] = text
            captured["reply_markup"] = reply_markup

    sender = TelegramSender(cast(Bot, _FakeBot()))
    await sender.send(123, "conditions are firing")

    assert captured["chat_id"] == 123
    assert captured["text"] == "conditions are firing"
    assert captured["reply_markup"] is None


@pytest.mark.asyncio
async def test_telegram_sender_translates_inline_keyboard() -> None:
    """A neutral keyboard becomes an InlineKeyboardMarkup with the right buttons."""
    captured: dict[str, object] = {}

    class _FakeBot:
        async def send_message(
            self, *, chat_id: int, text: str, reply_markup: object = None
        ) -> None:
            captured["reply_markup"] = reply_markup

    sender = TelegramSender(cast(Bot, _FakeBot()))
    keyboard: InlineKeyboard = [
        [
            InlineButton(text="👍", callback_data="fb:up:42"),
            InlineButton(text="👎", callback_data="fb:down:42"),
        ]
    ]
    await sender.send(5, "rate this", keyboard=keyboard)

    markup = captured["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    rows = markup.inline_keyboard
    assert len(rows) == 1
    assert [button.text for button in rows[0]] == ["👍", "👎"]
    assert [button.callback_data for button in rows[0]] == ["fb:up:42", "fb:down:42"]
