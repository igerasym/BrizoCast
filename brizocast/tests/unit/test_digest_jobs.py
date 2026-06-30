"""Unit tests for the digest jobs and shared digest buffer (task 8.5).

Covers Requirements 10.5, 10.6, 10.7, and 10.8 with in-memory fakes:

* an empty period sends nothing (Req 10.8);
* the morning and evening digests send one summary per subscription listing its
  qualifying buffered scores (Req 10.5, 10.6);
* the weekly-best-day digest sends a summary naming the best forecast day
  (Req 10.7);
* the shared :class:`DigestBuffer` append/drain hand-off behaves as the
  forecast-check job (producer) and digest jobs (consumers) rely on.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from brizocast.bot.formatters.digests import format_digest
from brizocast.core.domain.forecast import ForecastWindow
from brizocast.core.domain.scoring import ScoreBreakdown, ScoreCategory, ScoreResult
from brizocast.core.domain.scoring_types import FactorContribution
from brizocast.core.domain.spot import SurfSpot
from brizocast.notifications.modes import DigestItem, NotificationMode
from brizocast.notifications.sender import (
    InlineKeyboard,
    RetryingNotificationSender,
    SendRequest,
)
from brizocast.scheduler.digest_jobs import (
    DigestBuffer,
    DigestJobRunner,
    DigestTarget,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fakes & helpers
# --------------------------------------------------------------------------- #
class _RecordingSender:
    """A :class:`MessageSender` that records every delivered message."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
    ) -> None:
        self.sent.append((chat_id, text))


class _FailingSender:
    """A :class:`MessageSender` that always raises, to exercise resilience."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
    ) -> None:
        self.attempts += 1
        raise RuntimeError("boom")


class _FakeSource:
    """In-memory :class:`DigestSubscriptionSource` keyed by mode."""

    def __init__(self, by_mode: dict[NotificationMode, list[DigestTarget]]) -> None:
        self._by_mode = by_mode

    async def targets_for_mode(
        self, mode: NotificationMode
    ) -> Sequence[DigestTarget]:
        return self._by_mode.get(mode, [])


def _runner(
    buffer: DigestBuffer,
    source: _FakeSource,
    sender: RetryingNotificationSender,
    *,
    now: datetime | None = None,
) -> DigestJobRunner:
    fixed = now or datetime(2025, 6, 8, 7, 0, tzinfo=UTC)
    return DigestJobRunner(
        buffer=buffer,
        subscriptions=source,
        sender=sender,
        now=lambda: fixed,
    )


def _retrying(sender: object) -> RetryingNotificationSender:
    return RetryingNotificationSender(sender, retry_count=1)  # type: ignore[arg-type]


def _item(spot_key: str, score: int, start: datetime) -> DigestItem:
    window = ForecastWindow(start=start, end=start + timedelta(hours=3))
    zero = FactorContribution(value=0.0, weight=0.2)
    breakdown = ScoreBreakdown(
        wave_height=zero,
        swell_period=zero,
        wind_speed=zero,
        wind_direction=zero,
        swell_direction=zero,
        total_weighted=0.0,
    )
    result = ScoreResult(
        score=score,
        category=ScoreCategory.from_score(score),
        breakdown=breakdown,
        forecast_window=window,
    )
    spot = SurfSpot(spot_key=spot_key, name=spot_key.upper(), lat=39.3, lon=-9.3)
    return DigestItem(spot=spot, score_result=result)


# --------------------------------------------------------------------------- #
# DigestBuffer hand-off
# --------------------------------------------------------------------------- #
def test_buffer_append_then_drain_returns_items_in_order() -> None:
    buffer = DigestBuffer()
    a = _item("pt/a", 80, datetime(2025, 6, 1, 7, tzinfo=UTC))
    b = _item("pt/b", 70, datetime(2025, 6, 1, 9, tzinfo=UTC))
    buffer.append(1, [a])
    buffer.append(1, [b])
    assert buffer.drain(1) == [a, b]


def test_buffer_drain_clears_buffer() -> None:
    buffer = DigestBuffer()
    buffer.append(1, [_item("pt/a", 80, datetime(2025, 6, 1, 7, tzinfo=UTC))])
    buffer.drain(1)
    assert buffer.drain(1) == []


def test_buffer_append_empty_is_noop() -> None:
    buffer = DigestBuffer()
    buffer.append(1, [])
    assert buffer.pending_subscriptions() == []
    assert buffer.drain(1) == []


def test_buffer_isolates_subscriptions() -> None:
    buffer = DigestBuffer()
    buffer.append(1, [_item("pt/a", 80, datetime(2025, 6, 1, 7, tzinfo=UTC))])
    buffer.append(2, [_item("pt/b", 70, datetime(2025, 6, 1, 8, tzinfo=UTC))])
    assert {*buffer.pending_subscriptions()} == {1, 2}
    buffer.drain(1)
    assert buffer.pending_subscriptions() == [2]


def test_buffer_peek_does_not_drain() -> None:
    buffer = DigestBuffer()
    buffer.append(1, [_item("pt/a", 80, datetime(2025, 6, 1, 7, tzinfo=UTC))])
    assert len(buffer.peek(1)) == 1
    assert len(buffer.drain(1)) == 1


# --------------------------------------------------------------------------- #
# Empty period sends nothing (Req 10.8)
# --------------------------------------------------------------------------- #
async def test_empty_period_sends_nothing() -> None:
    buffer = DigestBuffer()  # nothing buffered
    source = _FakeSource(
        {NotificationMode.MORNING_DIGEST: [DigestTarget(subscription_id=1, chat_id=111)]}
    )
    inner = _RecordingSender()
    results = await _runner(buffer, source, _retrying(inner)).run_morning_digest()
    assert results == []
    assert inner.sent == []


async def test_only_subrideable_scores_send_nothing() -> None:
    buffer = DigestBuffer()
    # A sub-Rideable score (<50) is filtered by build_digest -> nothing to send.
    buffer.append(1, [_item("pt/a", 30, datetime(2025, 6, 7, 8, tzinfo=UTC))])
    source = _FakeSource(
        {NotificationMode.EVENING_DIGEST: [DigestTarget(subscription_id=1, chat_id=111)]}
    )
    inner = _RecordingSender()
    results = await _runner(buffer, source, _retrying(inner)).run_evening_digest()
    assert results == []
    assert inner.sent == []
    # The buffer was still drained even though nothing was sent.
    assert buffer.drain(1) == []


# --------------------------------------------------------------------------- #
# Morning / evening digests list qualifying items (Req 10.5, 10.6)
# --------------------------------------------------------------------------- #
async def test_morning_digest_sends_one_summary_listing_items() -> None:
    buffer = DigestBuffer()
    buffer.append(
        1,
        [
            _item("pt/peak", 88, datetime(2025, 6, 8, 6, tzinfo=UTC)),
            _item("pt/cove", 72, datetime(2025, 6, 8, 9, tzinfo=UTC)),
        ],
    )
    source = _FakeSource(
        {NotificationMode.MORNING_DIGEST: [DigestTarget(subscription_id=1, chat_id=555)]}
    )
    inner = _RecordingSender()
    results = await _runner(buffer, source, _retrying(inner)).run_morning_digest()

    assert len(results) == 1
    assert results[0].delivered is True
    assert len(inner.sent) == 1
    chat_id, text = inner.sent[0]
    assert chat_id == 555
    assert "Morning digest" in text
    assert "PT/PEAK" in text and "Score 88" in text
    assert "PT/COVE" in text and "Score 72" in text
    # Buffer drained after the run.
    assert buffer.drain(1) == []


async def test_evening_digest_sends_per_subscription_and_skips_empty() -> None:
    buffer = DigestBuffer()
    buffer.append(1, [_item("pt/a", 80, datetime(2025, 6, 8, 17, tzinfo=UTC))])
    # Subscription 2 has nothing buffered -> must be skipped (Req 10.8).
    source = _FakeSource(
        {
            NotificationMode.EVENING_DIGEST: [
                DigestTarget(subscription_id=1, chat_id=111),
                DigestTarget(subscription_id=2, chat_id=222),
            ]
        }
    )
    inner = _RecordingSender()
    results = await _runner(buffer, source, _retrying(inner)).run_evening_digest()

    assert len(results) == 1
    assert [chat for chat, _ in inner.sent] == [111]
    assert "Evening digest" in inner.sent[0][1]


# --------------------------------------------------------------------------- #
# Weekly best day (Req 10.7)
# --------------------------------------------------------------------------- #
async def test_weekly_digest_sends_best_day_only() -> None:
    buffer = DigestBuffer()
    buffer.append(
        1,
        [
            # Day 1: max 75
            _item("d1/am", 60, datetime(2025, 6, 1, 7, tzinfo=UTC)),
            _item("d1/noon", 75, datetime(2025, 6, 1, 12, tzinfo=UTC)),
            # Day 2: max 92 -> best day
            _item("d2/dawn", 80, datetime(2025, 6, 2, 6, tzinfo=UTC)),
            _item("d2/peak", 92, datetime(2025, 6, 2, 11, tzinfo=UTC)),
        ],
    )
    source = _FakeSource(
        {NotificationMode.WEEKLY_BEST_DAY: [DigestTarget(subscription_id=1, chat_id=777)]}
    )
    inner = _RecordingSender()
    results = await _runner(buffer, source, _retrying(inner)).run_weekly_digest()

    assert len(results) == 1
    chat_id, text = inner.sent[0]
    assert chat_id == 777
    assert "Best surf day this week" in text
    assert "Best day: 2025-06-02" in text
    # Only day 2's items appear; day 1 is excluded.
    assert "D2/DAWN" in text and "D2/PEAK" in text
    assert "D1/AM" not in text and "D1/NOON" not in text


# --------------------------------------------------------------------------- #
# No targets at all
# --------------------------------------------------------------------------- #
async def test_no_targets_sends_nothing() -> None:
    buffer = DigestBuffer()
    source = _FakeSource({})
    inner = _RecordingSender()
    results = await _runner(buffer, source, _retrying(inner)).run_morning_digest()
    assert results == []
    assert inner.sent == []


# --------------------------------------------------------------------------- #
# Delivery resilience: one failing send does not abort the batch (Req 18.3)
# --------------------------------------------------------------------------- #
async def test_failed_delivery_is_reported_not_raised() -> None:
    buffer = DigestBuffer()
    buffer.append(1, [_item("pt/a", 80, datetime(2025, 6, 8, 7, tzinfo=UTC))])
    source = _FakeSource(
        {NotificationMode.MORNING_DIGEST: [DigestTarget(subscription_id=1, chat_id=111)]}
    )
    results = await _runner(buffer, source, _retrying(_FailingSender())).run_morning_digest()
    assert len(results) == 1
    assert results[0].delivered is False
    # The failed summary carries the subscription id for digest-fallback routing.
    assert results[0].request.ref == 1


# --------------------------------------------------------------------------- #
# Formatter sanity (pure)
# --------------------------------------------------------------------------- #
def test_format_digest_renders_header_and_lines() -> None:
    from brizocast.notifications.modes import build_digest

    items = [_item("pt/a", 88, datetime(2025, 6, 8, 6, tzinfo=UTC))]
    digest = build_digest(NotificationMode.MORNING_DIGEST, items, _period(items))
    assert digest is not None
    text = format_digest(digest)
    assert text.splitlines()[0] == "🌅 Morning digest"
    assert "Score 88 (Excellent)" in text


def _period(items: list[DigestItem]):  # type: ignore[no-untyped-def]
    from brizocast.notifications.modes import DigestPeriod

    start = min(i.timestamp for i in items)
    return DigestPeriod(start=start, end=start)
