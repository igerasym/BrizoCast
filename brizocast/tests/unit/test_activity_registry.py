"""Unit tests for the Activity abstraction, registry, and SurfActivity.

Covers the multi-sport extensibility surface (Req 1.3, 8.10, 17.1-17.4, 17.6;
supports Property 31): the surf activity conforms to the common abstraction, is
registered and retrievable by key, the surf scorer structurally satisfies the
:class:`Scorer` port, ``available()`` excludes non-MVP activities, and
``get()`` raises :class:`NotFoundError` for an unknown key.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from brizocast.activities.base import Activity
from brizocast.activities.bootstrap import register_builtin_activities
from brizocast.activities.registry import ActivityRegistry
from brizocast.activities.surf.activity import SurfActivity
from brizocast.activities.surf.conditions import SurfConditions
from brizocast.activities.surf.scorer import SurfScorer
from brizocast.core.domain.conditions import ConditionsModel
from brizocast.core.errors import NotFoundError
from brizocast.core.ports.scorer import Scorer

# Structural conformance is verified statically by mypy: a SurfScorer must be a
# valid Scorer[SurfConditions]. Assigning it to that annotated name fails type
# checking if the surf scorer ever drifts from the port's shape.
_scorer_port_check: Scorer[SurfConditions] = SurfScorer()


@pytest.fixture
def registered() -> None:
    """Ensure the built-in activities are registered before each test."""

    register_builtin_activities()


def test_surf_activity_conforms_to_abstraction(registered: None) -> None:
    surf = ActivityRegistry.get("surf")
    assert isinstance(surf, Activity)
    assert surf.key == "surf"
    assert surf.display_name == "🏄 Surf"
    assert surf.available_in_mvp is True


def test_surf_activity_exposes_scorer_schema_and_provider(registered: None) -> None:
    surf = SurfActivity()
    assert isinstance(surf.scorer(), SurfScorer)
    assert surf.conditions_schema() is SurfConditions
    assert issubclass(surf.conditions_schema(), ConditionsModel)
    assert surf.default_forecast_provider_key() == "open_meteo_marine"


def test_surf_scorer_satisfies_scorer_port(registered: None) -> None:
    # Runtime structural check (Scorer is @runtime_checkable).
    assert isinstance(ActivityRegistry.get("surf").scorer(), Scorer)


def test_get_unknown_key_raises_not_found(registered: None) -> None:
    with pytest.raises(NotFoundError):
        ActivityRegistry.get("snowboard")


def test_available_excludes_non_mvp_activities(registered: None) -> None:
    class FutureActivity(Activity[SurfConditions]):
        key: ClassVar[str] = "future-sport"
        display_name: ClassVar[str] = "Future"
        available_in_mvp: ClassVar[bool] = False

        def scorer(self) -> Scorer[SurfConditions]:
            return SurfScorer()

        def conditions_schema(self) -> type[SurfConditions]:
            return SurfConditions

        def default_forecast_provider_key(self) -> str:
            return "open_meteo_marine"

    ActivityRegistry.register(FutureActivity())

    available_keys = {activity.key for activity in ActivityRegistry.available()}
    all_keys = {activity.key for activity in ActivityRegistry.all()}

    assert "surf" in available_keys
    assert "future-sport" not in available_keys
    assert "future-sport" in all_keys


def test_register_is_idempotent_by_key(registered: None) -> None:
    before = len(ActivityRegistry.all())
    ActivityRegistry.register(SurfActivity())
    assert len(ActivityRegistry.all()) == before
