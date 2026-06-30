"""Surf-score categorization and weighted-score result types (pure, no I/O).

Maps an integer :term:`Surf_Score` (0..100) to a :class:`ScoreCategory` band
and defines the shared result types a :term:`Scorer` produces — the per-factor
:class:`ScoreBreakdown` and the overall :class:`ScoreResult`.

The category enum and its band mapping live here so the scorer, the anti-spam
policy, and the alert formatter share a single source of truth for the bands.
:class:`ScoreResult` and :class:`ScoreBreakdown` live here too (alongside the
category) so that both the activity scorers and the explainable-alert formatter
can depend on a common result shape without importing any concrete scorer; the
``Scorer`` port references these types as well. The activity-specific factor
curves and the ``SurfScorer`` that *produces* these results live with the surf
activity (task 2.7).

The bands partition the full ``0..100`` integer range with mutually-exclusive,
jointly-exhaustive coverage:

============  ==========
Category      Score band
============  ==========
``PERFECT``   95..100
``EXCELLENT`` 85..94
``GOOD``      70..84
``RIDEABLE``  50..69
``IGNORE``    below 50
============  ==========

:class:`ScoreCategory` is an :class:`enum.IntEnum` whose member values encode a
rank (``IGNORE`` < ``RIDEABLE`` < ``GOOD`` < ``EXCELLENT`` < ``PERFECT``) so
categories compare directly with the ordering operators. The anti-spam policy
relies on this to express rules such as "below Rideable" as
``category < ScoreCategory.RIDEABLE`` (task 2.12).

Requirements covered: 8.2, 8.3, 8.4, 8.5, 8.6 (Score_Category bands; supports
Property 2) and 8.11 (per-factor breakdown; supports Property 5).
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, ConfigDict, Field

from brizocast.core.domain.forecast import ForecastWindow
from brizocast.core.domain.scoring_types import FactorContribution

# Inclusive lower bounds of each non-Ignore band, expressed once so the mapping
# stays consistent with the documented partition and the scorer's clamp range.
PERFECT_MIN = 95
EXCELLENT_MIN = 85
GOOD_MIN = 70
RIDEABLE_MIN = 50


class ScoreCategory(IntEnum):
    """A label derived from a :term:`Surf_Score`, ranked for comparison.

    Member values are *ranks*, not scores: ``IGNORE`` is lowest and ``PERFECT``
    highest, so the standard comparison operators order categories from worst
    to best. Use :meth:`from_score` to derive a category from a numeric score.
    """

    IGNORE = 0
    RIDEABLE = 1
    GOOD = 2
    EXCELLENT = 3
    PERFECT = 4

    @classmethod
    def from_score(cls, score: int) -> "ScoreCategory":
        """Map an integer surf score to its :class:`ScoreCategory` band.

        The bands are jointly exhaustive over ``0..100`` and mutually
        exclusive: Perfect ``95..100``, Excellent ``85..94``, Good ``70..84``,
        Rideable ``50..69``, Ignore below ``50``.

        Scores outside ``0..100`` are handled consistently with the scorer,
        which clamps its result to ``0..100``: any value at or above the
        Perfect threshold (including ``> 100``) maps to :attr:`PERFECT`, and any
        value below the Rideable threshold (including negatives) maps to
        :attr:`IGNORE`.

        :param score: An integer surf score.
        :returns: The category band the score falls into.
        """

        if score >= PERFECT_MIN:
            return cls.PERFECT
        if score >= EXCELLENT_MIN:
            return cls.EXCELLENT
        if score >= GOOD_MIN:
            return cls.GOOD
        if score >= RIDEABLE_MIN:
            return cls.RIDEABLE
        return cls.IGNORE


class ScoreBreakdown(BaseModel):
    """Per-factor contributions that compose a weighted :term:`Surf_Score`.

    Holds one :class:`FactorContribution` for each of the five surf factors —
    wave height, swell period, wind speed, wind direction, and swell direction —
    plus ``total_weighted``, the sum of the factor contributions in ``[0, 1]``
    (i.e. the score before scaling to ``0..100``).

    The breakdown is always *complete*: every factor is present, and the factor
    weights sum to 1 (Req 8.11, supports Property 5). This holds even for a
    daylight-gated result, where every factor value is ``0`` but the weights are
    unchanged.
    """

    model_config = ConfigDict(frozen=True)

    wave_height: FactorContribution = Field(description="Wave-height factor contribution.")
    swell_period: FactorContribution = Field(description="Swell-period factor contribution.")
    wind_speed: FactorContribution = Field(description="Wind-speed factor contribution.")
    wind_direction: FactorContribution = Field(description="Wind-direction factor contribution.")
    swell_direction: FactorContribution = Field(
        description="Swell-direction factor contribution."
    )
    total_weighted: float = Field(
        ge=0.0,
        le=1.0,
        description="Sum of the weighted factor contributions in [0, 1] (pre-scaling).",
    )

    def contributions(self) -> dict[str, FactorContribution]:
        """Return the five factor contributions keyed by factor name.

        The returned mapping preserves the canonical factor order (wave height,
        swell period, wind speed, wind direction, swell direction) so callers
        such as the alert formatter can render the breakdown deterministically.
        """

        return {
            "wave_height": self.wave_height,
            "swell_period": self.swell_period,
            "wind_speed": self.wind_speed,
            "wind_direction": self.wind_direction,
            "swell_direction": self.swell_direction,
        }


class ScoreResult(BaseModel):
    """The outcome of scoring a single forecast step against conditions.

    Carries the integer :term:`Surf_Score` (``0..100``), its derived
    :class:`ScoreCategory`, the complete per-factor :class:`ScoreBreakdown`, and
    the :class:`ForecastWindow` the score applies to. The score and category are
    always consistent: ``category == ScoreCategory.from_score(score)``.
    """

    model_config = ConfigDict(frozen=True)

    score: int = Field(ge=0, le=100, description="Surf_Score as an integer in 0..100.")
    category: ScoreCategory = Field(description="Score band derived from the score.")
    breakdown: ScoreBreakdown = Field(description="Complete per-factor contribution breakdown.")
    forecast_window: ForecastWindow = Field(
        description="Forecast window the score applies to (anti-spam identity)."
    )
