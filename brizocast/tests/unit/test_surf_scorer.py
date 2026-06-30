"""Unit tests for the surf conditions schema and weighted scorer (task 2.7).

Exercises the pure factor curves, the ``SurfConditions`` wave-band validator,
the weighted ``SurfScorer`` (bounded integer score, complete breakdown with
weights summing to 1, ideal vs. poor conditions, the daylight gate), and a
monotonicity spot-check. The numbered correctness properties are covered
exhaustively by the Hypothesis tests in tasks 2.8-2.11; these are illustrative
example/edge-case checks.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from brizocast.activities.surf.conditions import SurfConditions, TidePreference
from brizocast.activities.surf.scorer import (
    NEUTRAL_DIRECTION_SCORE,
    WEIGHTS,
    SurfScorer,
    direction_match,
    period_curve,
    wave_height_curve,
    wind_speed_curve,
)
from brizocast.core.domain.daylight import DaylightInfo
from brizocast.core.domain.forecast import Forecast, ForecastStep
from brizocast.core.domain.scoring import ScoreCategory

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #
TS = datetime(2025, 6, 21, 12, 0, tzinfo=UTC)

ALL_DAY = DaylightInfo(
    sunrise=datetime(2025, 6, 21, 0, 0, tzinfo=UTC),
    sunset=datetime(2025, 6, 22, 0, 0, tzinfo=UTC),
)
NO_DAY = DaylightInfo(
    sunrise=datetime(2025, 6, 22, 0, 0, tzinfo=UTC),
    sunset=datetime(2025, 6, 21, 0, 0, tzinfo=UTC),
)


def _conditions(**overrides: object) -> SurfConditions:
    base: dict[str, object] = {
        "min_wave_m": 1.0,
        "max_wave_m": 3.0,
        "min_period_s": 8.0,
        "max_wind_kmh": 20.0,
        "preferred_wind_dir_deg": 90.0,
        "preferred_swell_dir_deg": 270.0,
    }
    base.update(overrides)
    return SurfConditions(**base)  # type: ignore[arg-type]


def _step(**overrides: object) -> ForecastStep:
    base: dict[str, object] = {
        "timestamp": TS,
        "wave_height_m": 2.0,
        "swell_period_s": 14.0,
        "swell_direction_deg": 270.0,
        "wind_speed_kmh": 0.0,
        "wind_direction_deg": 90.0,
    }
    base.update(overrides)
    return ForecastStep(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Factor curves
# --------------------------------------------------------------------------- #
def test_wave_height_curve_plateaus_inside_band() -> None:
    assert wave_height_curve(1.0, 1.0, 3.0) == 1.0
    assert wave_height_curve(2.0, 1.0, 3.0) == 1.0
    assert wave_height_curve(3.0, 1.0, 3.0) == 1.0


def test_wave_height_curve_ramps_below_band() -> None:
    assert wave_height_curve(0.0, 2.0, 3.0) == 0.0
    assert wave_height_curve(1.0, 2.0, 3.0) == pytest.approx(0.5)


def test_wave_height_curve_decays_above_band() -> None:
    # Band width 2 m -> falloff scale 2 m; 1 m overshoot -> 0.5.
    assert wave_height_curve(4.0, 1.0, 3.0) == pytest.approx(0.5)
    assert wave_height_curve(5.0, 1.0, 3.0) == pytest.approx(0.0)
    assert wave_height_curve(10.0, 1.0, 3.0) == 0.0


def test_wave_height_curve_degenerate_band_uses_min_falloff() -> None:
    # min == max -> falloff floor of 1 m above the point.
    assert wave_height_curve(2.0, 2.0, 2.0) == 1.0
    assert wave_height_curve(2.5, 2.0, 2.0) == pytest.approx(0.5)


def test_period_curve_increases_and_saturates() -> None:
    assert period_curve(0.0, 8.0) == 0.0
    # target = min + 6 = 14 s -> full credit at/after 14 s.
    assert period_curve(14.0, 8.0) == pytest.approx(1.0)
    assert period_curve(20.0, 8.0) == 1.0
    assert period_curve(7.0, 8.0) == pytest.approx(0.5)


def test_wind_speed_curve_decays_with_wind() -> None:
    assert wind_speed_curve(0.0, 20.0) == 1.0
    # scale = 20 * 1.5 = 30 -> at max (20) sub-score = 1 - 20/30.
    assert wind_speed_curve(20.0, 20.0) == pytest.approx(1.0 / 3.0)
    assert wind_speed_curve(30.0, 20.0) == pytest.approx(0.0)
    assert wind_speed_curve(50.0, 20.0) == 0.0


def test_direction_match_linear_in_angular_distance() -> None:
    assert direction_match(90.0, 90.0) == 1.0
    assert direction_match(180.0, 90.0) == pytest.approx(0.5)
    assert direction_match(270.0, 90.0) == pytest.approx(0.0)
    # Wrap-around: 350 deg vs 10 deg is 20 deg apart.
    assert direction_match(350.0, 10.0) == pytest.approx(1.0 - 20.0 / 180.0)


def test_direction_match_none_is_neutral_no_penalty() -> None:
    assert direction_match(123.0, None) == NEUTRAL_DIRECTION_SCORE == 1.0


# --------------------------------------------------------------------------- #
# Conditions schema
# --------------------------------------------------------------------------- #
def test_conditions_rejects_inverted_wave_band() -> None:
    with pytest.raises(ValueError, match="min_wave_m must be less than or equal"):
        _conditions(min_wave_m=3.0, max_wave_m=1.0)


def test_conditions_allows_equal_wave_bounds_and_optional_fields() -> None:
    c = _conditions(
        min_wave_m=2.0,
        max_wave_m=2.0,
        preferred_wind_dir_deg=None,
        preferred_swell_dir_deg=None,
        tide_preference=TidePreference.MID,
        daylight_only=True,
    )
    assert c.daylight_only is True
    assert c.tide_preference is TidePreference.MID
    assert c.preferred_wind_dir_deg is None


# --------------------------------------------------------------------------- #
# Weights
# --------------------------------------------------------------------------- #
def test_weights_sum_to_one() -> None:
    assert math.isclose(sum(WEIGHTS.values()), 1.0, abs_tol=1e-9)
    assert set(WEIGHTS) == {
        "wave_height",
        "swell_period",
        "wind_speed",
        "wind_direction",
        "swell_direction",
    }


# --------------------------------------------------------------------------- #
# Scorer behaviour
# --------------------------------------------------------------------------- #
def test_ideal_conditions_score_perfect() -> None:
    result = SurfScorer().score(_step(), _conditions(), ALL_DAY)
    assert result.score == 100
    assert result.category is ScoreCategory.PERFECT


def test_score_is_bounded_integer() -> None:
    result = SurfScorer().score(_step(), _conditions(), ALL_DAY)
    assert isinstance(result.score, int)
    assert 0 <= result.score <= 100


def test_breakdown_is_complete_with_weights_summing_to_one() -> None:
    result = SurfScorer().score(_step(), _conditions(), ALL_DAY)
    contributions = result.breakdown.contributions()
    assert set(contributions) == set(WEIGHTS)
    total_weight = sum(c.weight for c in contributions.values())
    assert math.isclose(total_weight, 1.0, abs_tol=1e-9)
    # Category is consistent with the score band.
    assert result.category is ScoreCategory.from_score(result.score)


def test_intermediate_score_strictly_between_0_and_100_is_achievable() -> None:
    # Mediocre-but-rideable: small waves, short period, strong wind, offshore-ish.
    step = _step(
        wave_height_m=0.5,
        swell_period_s=6.0,
        wind_speed_kmh=18.0,
        wind_direction_deg=140.0,
        swell_direction_deg=230.0,
    )
    result = SurfScorer().score(step, _conditions(), ALL_DAY)
    assert 0 < result.score < 100


def test_daylight_gate_zeroes_out_when_outside_daylight() -> None:
    conditions = _conditions(daylight_only=True)
    result = SurfScorer().score(_step(), conditions, NO_DAY)
    assert result.score == 0
    assert result.category is ScoreCategory.IGNORE
    # Breakdown stays complete: all values zero, weights still sum to 1.
    contributions = result.breakdown.contributions()
    assert all(c.value == 0.0 for c in contributions.values())
    assert math.isclose(sum(c.weight for c in contributions.values()), 1.0, abs_tol=1e-9)
    assert result.breakdown.total_weighted == 0.0


def test_daylight_gate_not_applied_when_flag_disabled() -> None:
    result = SurfScorer().score(_step(), _conditions(daylight_only=False), NO_DAY)
    assert result.score == 100


def test_daylight_gate_scores_normally_within_daylight() -> None:
    result = SurfScorer().score(_step(), _conditions(daylight_only=True), ALL_DAY)
    assert result.score == 100


def test_monotonicity_improving_wind_never_decreases_score() -> None:
    scorer = SurfScorer()
    conditions = _conditions()
    base_step = _step(wind_speed_kmh=25.0)
    prev = scorer.score(base_step, conditions, ALL_DAY).score
    # Lowering wind (the favourable direction) must never decrease the score.
    for wind in (20.0, 15.0, 10.0, 5.0, 0.0):
        current = scorer.score(_step(wind_speed_kmh=wind), conditions, ALL_DAY).score
        assert current >= prev
        prev = current


def test_score_series_uses_resolver_for_daylight_gate() -> None:
    scorer = SurfScorer()
    conditions = _conditions(daylight_only=True)
    night_ts = datetime(2025, 6, 21, 2, 0, tzinfo=UTC)
    forecast = Forecast(
        spot_id="spot-1",
        steps=[_step(timestamp=TS), _step(timestamp=night_ts)],
    )

    def resolver(_ts: datetime) -> DaylightInfo:
        # Daylight only between 06:00 and 20:00 UTC on the day.
        return DaylightInfo(
            sunrise=datetime(2025, 6, 21, 6, 0, tzinfo=UTC),
            sunset=datetime(2025, 6, 21, 20, 0, tzinfo=UTC),
        )

    results = scorer.score_series(forecast, conditions, resolver)
    assert len(results) == 2
    assert results[0].score == 100  # noon -> daylight
    assert results[1].score == 0  # 02:00 -> gated to Ignore
    assert results[1].category is ScoreCategory.IGNORE


def test_score_series_without_resolver_treats_all_as_daylight() -> None:
    scorer = SurfScorer()
    conditions = _conditions(daylight_only=True)
    night_ts = datetime(2025, 6, 21, 2, 0, tzinfo=UTC)
    forecast = Forecast(spot_id="spot-1", steps=[_step(timestamp=night_ts)])
    results = scorer.score_series(forecast, conditions)
    assert results[0].score == 100
