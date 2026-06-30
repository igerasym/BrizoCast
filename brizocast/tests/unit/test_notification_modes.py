"""Unit tests for notification modes and digest selection (task 5.3).

Covers the :class:`NotificationMode` enum's alignment with the config string
keys, the empty-period "send nothing" rule (Req 10.8), morning/evening digests
listing qualifying scores chronologically (Req 10.5, 10.6), and the
weekly-best-day digest picking the day with the highest maximum surf score
(Req 10.7).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brizocast.config.settings import (
    ALL_NOTIFICATION_MODES,
    NOTIFICATION_MODE_EVENING_DIGEST,
    NOTIFICATION_MODE_IMMEDIATE,
    NOTIFICATION_MODE_MORNING_DIGEST,
    NOTIFICATION_MODE_WEEKLY_BEST_DAY,
)
from brizocast.core.domain.forecast import ForecastWindow
from brizocast.core.domain.scoring import ScoreBreakdown, ScoreCategory, ScoreResult
from brizocast.core.domain.scoring_types import FactorContribution
from brizocast.core.domain.spot import SurfSpot
from brizocast.notifications.modes import (
    Digest,
    DigestItem,
    DigestPeriod,
    NotificationMode,
    build_digest,
    select_recent,
    select_weekly_best_day,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Test helpers
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


def _item(spot_key: str, score: int, start: datetime) -> DigestItem:
    window = ForecastWindow(start=start, end=start + timedelta(hours=3))
    result = ScoreResult(
        score=score,
        category=ScoreCategory.from_score(score),
        breakdown=_zeroed_breakdown(),
        forecast_window=window,
    )
    spot = SurfSpot(spot_key=spot_key, name=spot_key.upper(), lat=39.3, lon=-9.3)
    return DigestItem(spot=spot, score_result=result)


_PERIOD = DigestPeriod(
    start=datetime(2025, 6, 1, tzinfo=UTC),
    end=datetime(2025, 6, 8, tzinfo=UTC),
)


# --------------------------------------------------------------------------- #
# NotificationMode enum
# --------------------------------------------------------------------------- #
def test_mode_values_align_with_config_keys() -> None:
    assert NotificationMode.IMMEDIATE == NOTIFICATION_MODE_IMMEDIATE
    assert NotificationMode.MORNING_DIGEST == NOTIFICATION_MODE_MORNING_DIGEST
    assert NotificationMode.EVENING_DIGEST == NOTIFICATION_MODE_EVENING_DIGEST
    assert NotificationMode.WEEKLY_BEST_DAY == NOTIFICATION_MODE_WEEKLY_BEST_DAY
    assert {m.value for m in NotificationMode} == set(ALL_NOTIFICATION_MODES)


def test_from_key_round_trips_persisted_string() -> None:
    for key in ALL_NOTIFICATION_MODES:
        assert NotificationMode.from_key(key) == key


def test_from_key_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        NotificationMode.from_key("does_not_exist")


def test_is_digest_flag() -> None:
    assert not NotificationMode.IMMEDIATE.is_digest
    assert NotificationMode.MORNING_DIGEST.is_digest
    assert NotificationMode.EVENING_DIGEST.is_digest
    assert NotificationMode.WEEKLY_BEST_DAY.is_digest


# --------------------------------------------------------------------------- #
# Empty period -> no digest (Req 10.8)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mode",
    [
        NotificationMode.MORNING_DIGEST,
        NotificationMode.EVENING_DIGEST,
        NotificationMode.WEEKLY_BEST_DAY,
    ],
)
def test_empty_buffer_yields_no_digest(mode: NotificationMode) -> None:
    assert build_digest(mode, [], _PERIOD) is None


@pytest.mark.parametrize(
    "mode",
    [NotificationMode.MORNING_DIGEST, NotificationMode.WEEKLY_BEST_DAY],
)
def test_only_subrideable_scores_yield_no_digest(mode: NotificationMode) -> None:
    # A score below 50 is IGNORE (below Rideable) and must not produce a digest.
    items = [_item("pt/a", 30, datetime(2025, 6, 1, 6, tzinfo=UTC))]
    assert build_digest(mode, items, _PERIOD) is None


def test_immediate_mode_is_not_digest_driven() -> None:
    with pytest.raises(ValueError):
        build_digest(NotificationMode.IMMEDIATE, [], _PERIOD)


# --------------------------------------------------------------------------- #
# Morning / evening digest lists qualifying items chronologically (Req 10.5/10.6)
# --------------------------------------------------------------------------- #
def test_recent_digest_lists_qualifying_items_in_order() -> None:
    items = [
        _item("pt/c", 72, datetime(2025, 6, 2, 9, tzinfo=UTC)),
        _item("pt/a", 88, datetime(2025, 6, 1, 7, tzinfo=UTC)),
        _item("pt/b", 40, datetime(2025, 6, 1, 8, tzinfo=UTC)),  # sub-Rideable, dropped
    ]
    digest = build_digest(NotificationMode.MORNING_DIGEST, items, _PERIOD)
    assert isinstance(digest, Digest)
    assert digest.mode is NotificationMode.MORNING_DIGEST
    # Sub-Rideable dropped; remaining ordered by timestamp.
    assert [i.spot.spot_key for i in digest.items] == ["pt/a", "pt/c"]


def test_select_recent_filters_and_sorts() -> None:
    items = [
        _item("z", 95, datetime(2025, 6, 3, 6, tzinfo=UTC)),
        _item("a", 51, datetime(2025, 6, 1, 6, tzinfo=UTC)),
    ]
    selected = select_recent(items)
    assert [i.spot.spot_key for i in selected] == ["a", "z"]


# --------------------------------------------------------------------------- #
# Weekly best day picks the highest-scoring day (Req 10.7)
# --------------------------------------------------------------------------- #
def test_weekly_best_day_picks_highest_max_score_day() -> None:
    items = [
        # Day 1: max 75
        _item("d1/morning", 60, datetime(2025, 6, 1, 7, tzinfo=UTC)),
        _item("d1/noon", 75, datetime(2025, 6, 1, 12, tzinfo=UTC)),
        # Day 2: max 92 -> best day
        _item("d2/dawn", 80, datetime(2025, 6, 2, 6, tzinfo=UTC)),
        _item("d2/peak", 92, datetime(2025, 6, 2, 11, tzinfo=UTC)),
        # Day 3: max 70
        _item("d3/only", 70, datetime(2025, 6, 3, 9, tzinfo=UTC)),
    ]
    digest = build_digest(NotificationMode.WEEKLY_BEST_DAY, items, _PERIOD)
    assert isinstance(digest, Digest)
    keys = [i.spot.spot_key for i in digest.items]
    # Only day 2's items, chronologically ordered.
    assert keys == ["d2/dawn", "d2/peak"]
    assert max(i.surf_score for i in digest.items) == 92


def test_weekly_best_day_tie_breaks_to_earliest_day() -> None:
    items = [
        _item("late/peak", 90, datetime(2025, 6, 5, 10, tzinfo=UTC)),
        _item("early/peak", 90, datetime(2025, 6, 2, 10, tzinfo=UTC)),
    ]
    selected = select_weekly_best_day(items)
    # Both days max at 90; earliest day wins.
    assert [i.spot.spot_key for i in selected] == ["early/peak"]


def test_weekly_best_day_handles_naive_timestamps() -> None:
    # Naive datetimes are treated as UTC and grouped by day consistently.
    items = [
        _item("d1", 70, datetime(2025, 6, 1, 9)),
        _item("d2", 85, datetime(2025, 6, 2, 9)),
    ]
    selected = select_weekly_best_day(items)
    assert [i.spot.spot_key for i in selected] == ["d2"]
