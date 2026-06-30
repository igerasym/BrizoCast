"""``Scorer`` port.

Protocol describing an activity's scoring engine: the shape the
:class:`~brizocast.activities.base.Activity` abstraction resolves and the
scheduler invokes to turn a forecast into a :class:`ScoreResult`. The MVP's
only implementation is the surf
:class:`~brizocast.activities.surf.scorer.SurfScorer`, but the port keeps the
scheduler and notification engine independent of any concrete activity so a new
sport supplies its own scorer without touching existing code (Req 17.1, 17.6,
8.10).

The protocol is **generic and contravariant** in its condition type: a concrete
scorer that only understands one activity's condition schema (e.g. a surf scorer
accepting :class:`SurfConditions`) structurally satisfies
``Scorer[SurfConditions]``. The condition argument is an *input*, so the type
variable is contravariant — a scorer for a narrower condition type is not a
scorer for the cross-activity :class:`ConditionsModel` base, which is exactly
what the type system should enforce.

This module is intentionally import-light and depends only on pure domain value
objects and ``typing`` — it never imports a concrete activity package, so the
core stays free of activity dependencies.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol, TypeVar, runtime_checkable

from brizocast.core.domain.conditions import ConditionsModel
from brizocast.core.domain.daylight import DaylightInfo
from brizocast.core.domain.forecast import Forecast, ForecastStep
from brizocast.core.domain.scoring import ScoreResult

# A callable resolving a step's timestamp to the daylight info for that day at
# the spot's location. The scheduler builds one from ``compute_daylight`` and the
# spot coordinates; the scorer stays pure and never looks up coordinates itself.
DaylightResolver = Callable[[datetime], DaylightInfo]

# Contravariant because the condition schema is consumed (an input parameter):
# a ``Scorer[SurfConditions]`` is NOT substitutable for a
# ``Scorer[ConditionsModel]``.
ConditionsT_contra = TypeVar("ConditionsT_contra", bound=ConditionsModel, contravariant=True)


@runtime_checkable
class Scorer(Protocol[ConditionsT_contra]):
    """An activity's scoring engine: scores forecast steps against conditions.

    Implementations are pure and stateless: scoring the same step and conditions
    always yields the same :class:`ScoreResult`. The activity registry resolves
    the scorer for a subscription's activity, so the scheduler depends only on
    this port rather than any concrete scorer (Req 17.6).
    """

    def score(
        self,
        step: ForecastStep,
        conditions: ConditionsT_contra,
        daylight: DaylightInfo,
    ) -> ScoreResult:
        """Score a single forecast ``step`` against ``conditions``."""
        ...

    def score_series(
        self,
        forecast: Forecast,
        conditions: ConditionsT_contra,
        daylight_resolver: DaylightResolver | None = None,
    ) -> list[ScoreResult]:
        """Score every step of ``forecast`` against ``conditions``.

        ``daylight_resolver`` maps a step timestamp to the daylight info for that
        day at the forecast's spot; callers relying on a daylight-only gate must
        supply one.
        """
        ...
