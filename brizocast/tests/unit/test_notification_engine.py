"""Unit tests for the NotificationEngine gating and routing (task 5.1).

Covers the gating semantics (Property 8, Req 11.2-11.6), anti-spam integration
(Req 9.x), and mode routing (Req 10.3) with in-memory fakes — no Telegram, no
database:

* muted suppresses all notifications (Req 11.3);
* snooze suppresses until it elapses, then notifications resume (Req 11.4, 11.5);
* a muted subscription stays suppressed after the snooze elapses (Req 11.6);
* quiet hours defer immediate alerts to the digest, including a window that
  wraps midnight (Req 11.2, 10.3);
* an anti-spam ``SUPPRESS`` skips the candidate (Req 9.3, 9.4);
* a qualifying immediate candidate produces an :class:`ImmediateDispatch`;
* a digest-mode subscription buffers qualifying candidates instead of
  dispatching.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

import pytest

from brizocast.core.domain.antispam import AntiSpamConfig, NotificationDecision
from brizocast.core.domain.forecast import ForecastWindow
from brizocast.core.domain.scoring import ScoreBreakdown, ScoreCategory, ScoreResult
from brizocast.core.domain.scoring_types import FactorContribution
from brizocast.core.domain.spot import SurfSpot
from brizocast.notifications.engine import (
    NotificationEngine,
    NotificationPlan,
    in_quiet_hours,
    is_gated,
)
from brizocast.notifications.modes import DigestItem, NotificationMode

pytestmark = pytest.mark.unit

_NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
_CHAT_ID = 555
_SUB_ID = 1


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _FakeRecord:
    """Minimal prior-notification record exposing ``surf_score``."""

    surf_score: int


class _FakeHistory:
    """In-memory :class:`WindowHistoryLookup` keyed by dedup identity."""

    def __init__(self, records: dict[tuple[int, str, str], int] | None = None) -> None:
        self._records = records or {}
        self.lookups: list[tuple[int, str, str]] = []

    async def latest_for_window(
        self, subscription_id: int, spot_key: str, forecast_window_key: str
    ) -> _FakeRecord | None:
        self.lookups.append((subscription_id, spot_key, forecast_window_key))
        score = self._records.get((subscription_id, spot_key, forecast_window_key))
        return _FakeRecord(surf_score=score) if score is not None else None


@dataclass(frozen=True)
class _FakeSubscription:
    """Plain test double satisfying the ``GatedSubscription`` protocol."""

    id: int = _SUB_ID
    notification_mode: str = NotificationMode.IMMEDIATE.value
    muted: bool = False
    snooze_until: datetime | None = None
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _zeroed_breakdown() -> ScoreBreakdown:
    zero = FactorContribution(value=0.0, weight=0.2)
    return ScoreBreakdown(
        wave_height=zero,
        swell_period=zero,
        wind_speed=zero,
        wind_direction=zero,
        swell_direction=zero,
        total_weighted=0.0,
    )


def _item(score: int, *, spot_key: str = "pt/ericeira", start: datetime = _NOW) -> DigestItem:
    window = ForecastWindow(start=start, end=start + timedelta(hours=3))
    result = ScoreResult(
        score=score,
        category=ScoreCategory.from_score(score),
        breakdown=_zeroed_breakdown(),
        forecast_window=window,
    )
    spot = SurfSpot(spot_key=spot_key, name=spot_key.upper(), lat=39.0, lon=-9.4)
    return DigestItem(spot=spot, score_result=result)


def _engine(history: _FakeHistory) -> NotificationEngine:
    return NotificationEngine(history, AntiSpamConfig(significant_improvement=10))


# --------------------------------------------------------------------------- #
# Gating: mute (Req 11.3)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_muted_suppresses_all() -> None:
    history = _FakeHistory()
    plan = await _engine(history).process(
        _FakeSubscription(muted=True), [_item(85)], _NOW, chat_id=_CHAT_ID
    )
    assert isinstance(plan, NotificationPlan)
    assert plan.gated is True
    assert plan.immediate == ()
    assert plan.digest == ()
    # Gated subscriptions short-circuit before any history lookup.
    assert history.lookups == []


# --------------------------------------------------------------------------- #
# Gating: snooze (Req 11.4, 11.5)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_snooze_suppresses_until_elapsed() -> None:
    sub = _FakeSubscription(snooze_until=_NOW + timedelta(hours=1))
    plan = await _engine(_FakeHistory()).process(sub, [_item(90)], _NOW, chat_id=_CHAT_ID)
    assert plan.gated is True
    assert plan.is_empty


@pytest.mark.asyncio
async def test_notifications_resume_after_snooze_elapses() -> None:
    # Snooze expired one hour ago; subscription not muted -> resume (Req 11.5).
    sub = _FakeSubscription(snooze_until=_NOW - timedelta(hours=1))
    plan = await _engine(_FakeHistory()).process(sub, [_item(90)], _NOW, chat_id=_CHAT_ID)
    assert plan.gated is False
    assert len(plan.immediate) == 1


# --------------------------------------------------------------------------- #
# Gating: mute persists past snooze (Req 11.6)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_muted_stays_suppressed_after_snooze_elapses() -> None:
    sub = _FakeSubscription(muted=True, snooze_until=_NOW - timedelta(hours=1))
    plan = await _engine(_FakeHistory()).process(sub, [_item(95)], _NOW, chat_id=_CHAT_ID)
    assert plan.gated is True
    assert plan.is_empty


# --------------------------------------------------------------------------- #
# Quiet hours defer immediate alerts (Req 11.2, 10.3)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_quiet_hours_defers_immediate_to_digest() -> None:
    # now is 12:00; quiet hours 09:00-15:00 cover it.
    sub = _FakeSubscription(
        quiet_hours_start=time(9, 0), quiet_hours_end=time(15, 0)
    )
    item = _item(88)
    plan = await _engine(_FakeHistory()).process(sub, [item], _NOW, chat_id=_CHAT_ID)
    assert plan.gated is False
    assert plan.immediate == ()
    assert plan.digest == (item,)


@pytest.mark.asyncio
async def test_quiet_hours_midnight_wrap_defers_then_resumes() -> None:
    # Window 22:00 -> 06:00 wraps midnight.
    sub = _FakeSubscription(
        quiet_hours_start=time(22, 0), quiet_hours_end=time(6, 0)
    )

    # 23:00 is inside the wrapped window -> deferred to digest.
    night = datetime(2025, 6, 1, 23, 0, tzinfo=UTC)
    item_night = _item(82, start=night)
    night_plan = await _engine(_FakeHistory()).process(
        sub, [item_night], night, chat_id=_CHAT_ID
    )
    assert night_plan.immediate == ()
    assert night_plan.digest == (item_night,)

    # 12:00 is outside the window -> immediate dispatch.
    item_noon = _item(82, start=_NOW)
    noon_plan = await _engine(_FakeHistory()).process(
        sub, [item_noon], _NOW, chat_id=_CHAT_ID
    )
    assert len(noon_plan.immediate) == 1
    assert noon_plan.digest == ()


# --------------------------------------------------------------------------- #
# Anti-spam SUPPRESS skips (Req 9.3, 9.4)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_antispam_suppress_skips_candidate() -> None:
    item = _item(82)
    wkey = item.score_result.forecast_window.key()
    # Prior alert at 80; candidate 82 is +2, below the 10-point threshold.
    history = _FakeHistory({(_SUB_ID, item.spot.spot_key, wkey): 80})
    plan = await _engine(history).process(
        _FakeSubscription(), [item], _NOW, chat_id=_CHAT_ID
    )
    assert plan.immediate == ()
    assert plan.digest == ()
    assert history.lookups == [(_SUB_ID, item.spot.spot_key, wkey)]


@pytest.mark.asyncio
async def test_subrideable_candidate_skipped_without_lookup() -> None:
    history = _FakeHistory()
    plan = await _engine(history).process(
        _FakeSubscription(), [_item(30)], _NOW, chat_id=_CHAT_ID
    )
    assert plan.is_empty
    # Below Rideable is dropped before any history lookup (Req 9.1).
    assert history.lookups == []


# --------------------------------------------------------------------------- #
# Qualifying immediate produces a dispatch
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_qualifying_immediate_produces_dispatch() -> None:
    item = _item(88)
    plan = await _engine(_FakeHistory()).process(
        _FakeSubscription(), [item], _NOW, chat_id=_CHAT_ID
    )
    assert plan.gated is False
    assert plan.digest == ()
    assert len(plan.immediate) == 1
    dispatch = plan.immediate[0]
    assert dispatch.subscription_id == _SUB_ID
    assert dispatch.chat_id == _CHAT_ID
    assert dispatch.spot_key == item.spot.spot_key
    assert dispatch.score_result is item.score_result
    assert dispatch.decision is NotificationDecision.SEND_NEW


@pytest.mark.asyncio
async def test_improved_immediate_carries_send_improved() -> None:
    item = _item(95)
    wkey = item.score_result.forecast_window.key()
    history = _FakeHistory({(_SUB_ID, item.spot.spot_key, wkey): 80})  # +15 >= 10
    plan = await _engine(history).process(
        _FakeSubscription(), [item], _NOW, chat_id=_CHAT_ID
    )
    assert len(plan.immediate) == 1
    assert plan.immediate[0].decision is NotificationDecision.SEND_IMPROVED


# --------------------------------------------------------------------------- #
# Digest mode buffers instead of dispatching
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_digest_mode_buffers_qualifying_scores() -> None:
    sub = _FakeSubscription(notification_mode=NotificationMode.MORNING_DIGEST.value)
    item = _item(90)
    plan = await _engine(_FakeHistory()).process(sub, [item], _NOW, chat_id=_CHAT_ID)
    assert plan.gated is False
    assert plan.immediate == ()
    assert plan.digest == (item,)


# --------------------------------------------------------------------------- #
# Pure gating helpers
# --------------------------------------------------------------------------- #
def test_in_quiet_hours_same_day_window() -> None:
    start, end = time(9, 0), time(17, 0)
    assert in_quiet_hours(start, end, datetime(2025, 6, 1, 12, tzinfo=UTC))
    assert in_quiet_hours(start, end, datetime(2025, 6, 1, 9, tzinfo=UTC))  # inclusive start
    assert not in_quiet_hours(start, end, datetime(2025, 6, 1, 17, tzinfo=UTC))  # exclusive end
    assert not in_quiet_hours(start, end, datetime(2025, 6, 1, 8, tzinfo=UTC))


def test_in_quiet_hours_midnight_wrap() -> None:
    start, end = time(22, 0), time(6, 0)
    assert in_quiet_hours(start, end, datetime(2025, 6, 1, 23, tzinfo=UTC))
    assert in_quiet_hours(start, end, datetime(2025, 6, 1, 2, tzinfo=UTC))
    assert in_quiet_hours(start, end, datetime(2025, 6, 1, 22, tzinfo=UTC))  # inclusive start
    assert not in_quiet_hours(start, end, datetime(2025, 6, 1, 6, tzinfo=UTC))  # exclusive end
    assert not in_quiet_hours(start, end, datetime(2025, 6, 1, 12, tzinfo=UTC))


def test_in_quiet_hours_unset_or_empty() -> None:
    moment = datetime(2025, 6, 1, 12, tzinfo=UTC)
    assert not in_quiet_hours(None, time(6, 0), moment)
    assert not in_quiet_hours(time(9, 0), None, moment)
    assert not in_quiet_hours(time(9, 0), time(9, 0), moment)  # zero-length = off


def test_is_gated_helper() -> None:
    assert is_gated(_FakeSubscription(muted=True), _NOW)
    assert is_gated(_FakeSubscription(snooze_until=_NOW + timedelta(minutes=1)), _NOW)
    assert not is_gated(_FakeSubscription(), _NOW)
    assert not is_gated(_FakeSubscription(snooze_until=_NOW - timedelta(minutes=1)), _NOW)
