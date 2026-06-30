"""Weighted surf scoring engine and its pure factor curves (no I/O).

Implements :class:`SurfScorer`, a *weighted* scoring engine (not a pass/fail
threshold check, Req 8.7). Each of the five surf factors — wave height, swell
period, wind speed, wind direction, and swell direction — is normalized to a
sub-score in ``[0, 1]`` by a pure factor curve, the sub-scores are combined with
fixed :data:`WEIGHTS` that sum to 1, and the weighted total is scaled to an
integer :term:`Surf_Score` in ``0..100`` (Req 8.1).

The module is deliberately free of any imports from the forecast-retrieval,
notification, or persistence layers (Req 8.9): it depends only on pure domain
value objects (forecast steps, daylight, scoring result types) and the surf
condition schema. Other activities supply their own ``Scorer`` selected via the
activity registry, so adding a sport never touches this module (Req 8.10).

Design properties this implementation upholds:

* **Bounded integer score** — the result is always an ``int`` in ``0..100``
  (Property 1).
* **Weighted, monotonic combination** — because every weight is non-negative and
  the total is their weighted sum, improving a single factor's normalized
  sub-score while holding the others fixed never decreases the score; the
  factor curves are themselves monotonic in the favourable direction of their
  raw input, and intermediate scores strictly between 0 and 100 are achievable
  (Property 3).
* **Daylight gate** — when ``daylight_only`` is set and a step falls outside
  daylight, the result is score ``0`` / :attr:`ScoreCategory.IGNORE` with a
  zeroed (but still complete) breakdown (Req 8.8, Property 4).
* **Complete breakdown** — every result carries a contribution for all five
  factors and the weights sum to 1 (Req 8.11, Property 5).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from brizocast.activities.surf.conditions import SurfConditions
from brizocast.core.domain.daylight import DaylightInfo
from brizocast.core.domain.forecast import Forecast, ForecastStep, ForecastWindow
from brizocast.core.domain.scoring import ScoreBreakdown, ScoreCategory, ScoreResult
from brizocast.core.domain.scoring_types import FactorContribution

# --------------------------------------------------------------------------- #
# Weights (sum == 1.0). Tunable in a future iteration via config / feedback.
# --------------------------------------------------------------------------- #
WEIGHT_WAVE_HEIGHT: Final = 0.30
WEIGHT_SWELL_PERIOD: Final = 0.25
WEIGHT_WIND_SPEED: Final = 0.20
WEIGHT_WIND_DIRECTION: Final = 0.15
WEIGHT_SWELL_DIRECTION: Final = 0.10

WEIGHTS: Final[dict[str, float]] = {
    "wave_height": WEIGHT_WAVE_HEIGHT,
    "swell_period": WEIGHT_SWELL_PERIOD,
    "wind_speed": WEIGHT_WIND_SPEED,
    "wind_direction": WEIGHT_WIND_DIRECTION,
    "swell_direction": WEIGHT_SWELL_DIRECTION,
}

# Score bounds for the final integer Surf_Score.
SCORE_MIN: Final = 0
SCORE_MAX: Final = 100

# Sub-score returned by the direction factor when no preferred bearing is set:
# the absence of a preference imposes no penalty, so the factor is fully
# satisfied rather than dragging the score toward the middle.
NEUTRAL_DIRECTION_SCORE: Final = 1.0

# Swell period (seconds) above the configured minimum at which the period factor
# reaches full credit; below that it ramps up linearly with period.
PERIOD_FULL_CREDIT_MARGIN_S: Final = 6.0

# Wind-speed factor: full credit at calm, decaying to zero at this multiple of
# the maximum acceptable wind (so the maximum itself scores a low-but-nonzero
# "marginal" value rather than an immediate zero).
WIND_FALLOFF_MULTIPLIER: Final = 1.5

# Minimum wave-overshoot falloff scale (metres) used when the favourable band is
# degenerate (min == max), so the above-band decay is always well defined.
MIN_WAVE_FALLOFF_M: Final = 1.0

# A daylight interval spanning all representable time; used by ``score_series``
# when no daylight resolver is supplied so that no step is gated.
_ALWAYS_DAYLIGHT: Final = DaylightInfo(
    sunrise=datetime.min.replace(tzinfo=UTC),
    sunset=datetime.max.replace(tzinfo=UTC),
)

# A callable resolving a step's timestamp to the daylight info for that day at
# the spot's location. The scheduler builds one from ``compute_daylight`` and the
# spot coordinates; the scorer stays pure and never looks up coordinates itself.
DaylightResolver = Callable[[datetime], DaylightInfo]


def _clamp01(value: float) -> float:
    """Clamp ``value`` into the inclusive ``[0.0, 1.0]`` range."""

    return min(1.0, max(0.0, value))


def _angular_distance_deg(a: float, b: float) -> float:
    """Return the smallest absolute angular separation between two bearings.

    The result is in ``[0, 180]`` degrees and is symmetric in its arguments,
    correctly wrapping around the 0/360 boundary (e.g. 350 deg and 10 deg are
    20 deg apart).
    """

    diff = abs(a - b) % 360.0
    return min(diff, 360.0 - diff)


def wave_height_curve(wave_m: float, min_m: float, max_m: float) -> float:
    """Normalize wave height to ``[0, 1]`` with a plateau over the favourable band.

    The curve is ``1.0`` for any wave height within ``[min_m, max_m]``. Below the
    band it ramps up linearly from ``0`` at flat (``0 m``) to ``1`` at ``min_m``;
    above the band it decays linearly back toward ``0`` over a falloff scale
    equal to the band width (at least :data:`MIN_WAVE_FALLOFF_M`), so larger,
    blown-out surf scores progressively lower.

    Monotonic toward the band from either side: increasing an under-sized wave
    or decreasing an over-sized one never lowers the sub-score.
    """

    if wave_m < min_m:
        if min_m <= 0.0:
            return 1.0
        return _clamp01(wave_m / min_m)
    if wave_m <= max_m:
        return 1.0
    falloff = max(max_m - min_m, MIN_WAVE_FALLOFF_M)
    return _clamp01(1.0 - (wave_m - max_m) / falloff)


def period_curve(period_s: float, min_period_s: float) -> float:
    """Normalize swell period to ``[0, 1]``, rising with longer period.

    Longer-period groundswell is always more favourable than short-period wind
    chop, so the curve increases monotonically with ``period_s`` and saturates
    at ``1.0`` once the period reaches :data:`PERIOD_FULL_CREDIT_MARGIN_S`
    seconds beyond ``min_period_s``. The result is ``0`` only at zero period.
    """

    if period_s <= 0.0:
        return 0.0
    target = min_period_s + PERIOD_FULL_CREDIT_MARGIN_S
    if target <= 0.0:
        return 1.0
    return _clamp01(period_s / target)


def wind_speed_curve(wind_kmh: float, max_wind_kmh: float) -> float:
    """Normalize wind speed to ``[0, 1]``, decaying as wind rises.

    Lighter wind is always more favourable, so the curve is ``1.0`` at calm and
    decreases monotonically with ``wind_kmh``, reaching ``0`` at
    :data:`WIND_FALLOFF_MULTIPLIER` times the maximum acceptable wind. At exactly
    ``max_wind_kmh`` the sub-score is a low-but-nonzero "marginal" value.
    """

    if wind_kmh <= 0.0:
        return 1.0
    scale = max(max_wind_kmh, 1.0) * WIND_FALLOFF_MULTIPLIER
    return _clamp01(1.0 - wind_kmh / scale)


def direction_match(actual_deg: float, preferred_deg: float | None) -> float:
    """Normalize a bearing's match to a preferred direction into ``[0, 1]``.

    When ``preferred_deg`` is ``None`` there is no preference to match, so the
    factor returns :data:`NEUTRAL_DIRECTION_SCORE` (no penalty). Otherwise the
    sub-score decreases linearly with the angular distance to the preferred
    bearing: ``1.0`` when aligned, ``0.5`` at 90 deg off, and ``0`` when exactly
    opposite (180 deg).
    """

    if preferred_deg is None:
        return NEUTRAL_DIRECTION_SCORE
    distance = _angular_distance_deg(actual_deg, preferred_deg)
    return _clamp01(1.0 - distance / 180.0)


class SurfScorer:
    """Weighted surf :term:`Scorer` producing 0-100 scores with a breakdown.

    Stateless and pure: scoring the same step and conditions always yields the
    same result. Implements the ``Scorer`` shape (``score`` and ``score_series``)
    that the activity registry resolves for the surf activity.
    """

    def score(
        self,
        step: ForecastStep,
        conditions: SurfConditions,
        daylight: DaylightInfo,
    ) -> ScoreResult:
        """Score a single forecast ``step`` against ``conditions``.

        Applies the daylight gate first (Req 8.8): when ``conditions`` enable
        ``daylight_only`` and ``daylight`` reports the step's timestamp is
        outside daylight, the step scores ``0`` / :attr:`ScoreCategory.IGNORE`
        with a zeroed-but-complete breakdown. Otherwise it computes the five
        normalized factors, weight-combines them, scales to an integer
        ``0..100``, and derives the category from the score.

        The forecast window on the result is the single-instant window
        ``[timestamp, timestamp]`` for the step.
        """

        window = ForecastWindow(start=step.timestamp, end=step.timestamp)

        if conditions.daylight_only and not daylight.is_daylight(step.timestamp):
            return ScoreResult(
                score=SCORE_MIN,
                category=ScoreCategory.IGNORE,
                breakdown=self._zeroed_breakdown(),
                forecast_window=window,
            )

        f_wave = wave_height_curve(step.wave_height_m, conditions.min_wave_m, conditions.max_wave_m)
        f_period = period_curve(step.swell_period_s, conditions.min_period_s)
        f_wind = wind_speed_curve(step.wind_speed_kmh, conditions.max_wind_kmh)
        f_wind_dir = direction_match(step.wind_direction_deg, conditions.preferred_wind_dir_deg)
        f_swell_dir = direction_match(step.swell_direction_deg, conditions.preferred_swell_dir_deg)

        breakdown = self._build_breakdown(f_wave, f_period, f_wind, f_wind_dir, f_swell_dir)
        score = self._clamp_score(round(breakdown.total_weighted * SCORE_MAX))
        return ScoreResult(
            score=score,
            category=ScoreCategory.from_score(score),
            breakdown=breakdown,
            forecast_window=window,
        )

    def score_series(
        self,
        forecast: Forecast,
        conditions: SurfConditions,
        daylight_resolver: DaylightResolver | None = None,
    ) -> list[ScoreResult]:
        """Score every step of ``forecast`` against ``conditions``.

        ``daylight_resolver`` maps a step timestamp to the :class:`DaylightInfo`
        for that day at the forecast's spot; the scheduler builds one from
        ``compute_daylight`` and the spot's coordinates, keeping this method pure
        (a ``Forecast`` carries only ``spot_id``, not coordinates). When no
        resolver is supplied, every step is treated as daylight, so callers that
        rely on the ``daylight_only`` gate MUST pass a resolver.
        """

        resolve: DaylightResolver = daylight_resolver or (lambda _ts: _ALWAYS_DAYLIGHT)
        return [self.score(step, conditions, resolve(step.timestamp)) for step in forecast.steps]

    @staticmethod
    def _build_breakdown(
        f_wave: float,
        f_period: float,
        f_wind: float,
        f_wind_dir: float,
        f_swell_dir: float,
    ) -> ScoreBreakdown:
        """Assemble a complete :class:`ScoreBreakdown` from the five sub-scores."""

        wave = FactorContribution(value=f_wave, weight=WEIGHT_WAVE_HEIGHT)
        period = FactorContribution(value=f_period, weight=WEIGHT_SWELL_PERIOD)
        wind = FactorContribution(value=f_wind, weight=WEIGHT_WIND_SPEED)
        wind_dir = FactorContribution(value=f_wind_dir, weight=WEIGHT_WIND_DIRECTION)
        swell_dir = FactorContribution(value=f_swell_dir, weight=WEIGHT_SWELL_DIRECTION)
        total = (
            wave.weighted
            + period.weighted
            + wind.weighted
            + wind_dir.weighted
            + swell_dir.weighted
        )
        return ScoreBreakdown(
            wave_height=wave,
            swell_period=period,
            wind_speed=wind,
            wind_direction=wind_dir,
            swell_direction=swell_dir,
            total_weighted=_clamp01(total),
        )

    @staticmethod
    def _zeroed_breakdown() -> ScoreBreakdown:
        """Return a breakdown with zero factor values but the real weights.

        Used for the daylight-gated result: the score is ``0`` yet the breakdown
        stays complete and its weights still sum to 1 (Property 5).
        """

        return SurfScorer._build_breakdown(0.0, 0.0, 0.0, 0.0, 0.0)

    @staticmethod
    def _clamp_score(score: int) -> int:
        """Clamp an integer score into the inclusive ``0..100`` range."""

        return min(SCORE_MAX, max(SCORE_MIN, score))
