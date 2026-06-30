"""The :class:`Activity` abstraction (multi-sport extensibility).

Defines the common abstraction every supported sport implements (Req 17.1,
17.2). An :class:`Activity` ties together the three things the rest of the
system needs to process a subscription for that sport: its :class:`Scorer`, its
condition schema (a :class:`ConditionsModel` subclass), and the key of the
forecast provider it defaults to. Subscriptions reference an activity by its
``key`` and the scheduler resolves the matching activity via the
:class:`~brizocast.activities.registry.ActivityRegistry` (Req 17.6).

The class is generic in its condition type so an activity can declare exactly
which :class:`ConditionsModel` subclass its scorer and schema use (Req 17.4):
``SurfActivity`` is an ``Activity[SurfConditions]`` whose ``scorer()`` returns a
``Scorer[SurfConditions]``. A future sport defines its own condition schema
(e.g. snow depth, temperature) that differs entirely from surf's, and is typed
just as precisely — without touching this base or any existing activity
(Req 17.3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Generic, TypeVar

from brizocast.core.domain.conditions import ConditionsModel
from brizocast.core.ports.scorer import Scorer

# Each activity binds this to its own ``ConditionsModel`` subclass.
ConditionsT = TypeVar("ConditionsT", bound=ConditionsModel)


class Activity(ABC, Generic[ConditionsT]):
    """Common abstraction for a supported outdoor sport.

    Concrete activities set the three class attributes and implement the three
    abstract methods. ``key`` is the stable identifier persisted on a
    subscription; ``display_name`` is the user-facing label; ``available_in_mvp``
    marks whether the activity is selectable in the MVP (Req 1.3) — unavailable
    activities are still registered (so they are discoverable and reported as
    not-yet-supported) but excluded from
    :meth:`~brizocast.activities.registry.ActivityRegistry.available`.
    """

    key: ClassVar[str]
    display_name: ClassVar[str]
    available_in_mvp: ClassVar[bool]

    @abstractmethod
    def scorer(self) -> Scorer[ConditionsT]:
        """Return the activity's scoring engine."""
        ...

    @abstractmethod
    def conditions_schema(self) -> type[ConditionsT]:
        """Return the activity's condition schema type."""
        ...

    @abstractmethod
    def default_forecast_provider_key(self) -> str:
        """Return the key of the forecast provider this activity defaults to."""
        ...
