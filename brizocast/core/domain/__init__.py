"""Value objects and pure domain logic (no I/O): geo, scoring, anti-spam, daylight.

Re-exports the domain value objects so callers can import them directly from
``brizocast.core.domain`` regardless of which cohesive module they live in.
"""

from __future__ import annotations

from brizocast.core.domain.conditions import ConditionsModel, PresetParams
from brizocast.core.domain.daylight import DaylightInfo, compute_daylight
from brizocast.core.domain.forecast import Forecast, ForecastStep, ForecastWindow
from brizocast.core.domain.geo import GeoCandidate, GeoPoint
from brizocast.core.domain.scoring import ScoreBreakdown, ScoreCategory, ScoreResult
from brizocast.core.domain.scoring_types import FactorContribution
from brizocast.core.domain.spot import SurfSpot

__all__ = [
    "ConditionsModel",
    "DaylightInfo",
    "FactorContribution",
    "Forecast",
    "ForecastStep",
    "ForecastWindow",
    "GeoCandidate",
    "GeoPoint",
    "PresetParams",
    "ScoreBreakdown",
    "ScoreCategory",
    "ScoreResult",
    "SurfSpot",
    "compute_daylight",
]
