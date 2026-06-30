"""The surf :class:`Activity` plug-in.

Binds the surf sport's pieces together behind the cross-activity
:class:`~brizocast.activities.base.Activity` abstraction: the
:class:`~brizocast.activities.surf.scorer.SurfScorer`, the
:class:`~brizocast.activities.surf.conditions.SurfConditions` schema, and the
default forecast provider key (``"open_meteo_marine"``, the free no-key
Open-Meteo Marine source). Surf is the only activity available in the MVP
(Req 1.3).

Registering surf lives in :mod:`brizocast.activities.bootstrap`, not here, so
adding a future sport never edits this module (Req 17.3).
"""

from __future__ import annotations

from typing import ClassVar

from brizocast.activities.base import Activity
from brizocast.activities.surf.conditions import SurfConditions
from brizocast.activities.surf.scorer import SurfScorer
from brizocast.core.ports.scorer import Scorer


class SurfActivity(Activity[SurfConditions]):
    """The Surf activity: weighted surf scoring over Open-Meteo Marine forecasts."""

    key: ClassVar[str] = "surf"
    display_name: ClassVar[str] = "🏄 Surf"
    available_in_mvp: ClassVar[bool] = True

    def scorer(self) -> Scorer[SurfConditions]:
        """Return a fresh, stateless surf scorer."""

        return SurfScorer()

    def conditions_schema(self) -> type[SurfConditions]:
        """Return the surf condition schema type."""

        return SurfConditions

    def default_forecast_provider_key(self) -> str:
        """Return the surf default forecast provider key."""

        return "open_meteo_marine"
