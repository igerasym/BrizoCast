"""Unit tests for :class:`StatusService` and the scheduler-run state (task 7.7).

Exercise the service against in-memory fakes for the collaborating services and
a fake activity/scorer registered in the process-global
:class:`ActivityRegistry`, verifying:

* ``/status`` reporting — :meth:`StatusService.active_subscription_count` counts
  only active subscriptions and :meth:`StatusService.last_scheduler_run` reads
  the shared scheduler-run state, returning ``None`` until a run is recorded
  (Req 13.3);
* ``/forecast`` — :meth:`StatusService.best_forecast_for_subscription` returns
  the highest-scoring spot/score across nearby spots regardless of the
  subscription's mute/snooze state (Req 13.4), reports no-spots when nothing is
  within range, and skips spots whose forecast fetch fails (Req 6.5).

Together these support Property 28 (/status and /forecast reporting).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brizocast.activities.base import Activity
from brizocast.activities.registry import ActivityRegistry
from brizocast.activities.surf.conditions import SurfConditions
from brizocast.core.domain.conditions import ConditionsModel
from brizocast.core.domain.daylight import DaylightInfo
from brizocast.core.domain.forecast import Forecast, ForecastStep, ForecastWindow
from brizocast.core.domain.geo import GeoPoint
from brizocast.core.domain.scoring import (
    ScoreBreakdown,
    ScoreCategory,
    ScoreResult,
)
from brizocast.core.domain.scoring_types import FactorContribution
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.errors import NotFoundError, ProviderRequestError
from brizocast.core.ports.scorer import DaylightResolver
from brizocast.models.subscription import Subscription
from brizocast.services.scheduler_state import InMemorySchedulerState
from brizocast.services.status_service import StatusService
from brizocast.services.subscription_service import SubscriptionForecastTarget

pytestmark = pytest.mark.unit

_FAKE_ACTIVITY_KEY = "faketest"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _score_result(score: int) -> ScoreResult:
    """Build a :class:`ScoreResult` for ``score`` with a complete breakdown."""

    factor = FactorContribution(value=0.0, weight=0.2)
    breakdown = ScoreBreakdown(
        wave_height=factor,
        swell_period=factor,
        wind_speed=factor,
        wind_direction=factor,
        swell_direction=factor,
        total_weighted=0.0,
    )
    window = ForecastWindow(
        start=datetime(2025, 6, 1, tzinfo=UTC),
        end=datetime(2025, 6, 1, 1, tzinfo=UTC),
    )
    return ScoreResult(
        score=score,
        category=ScoreCategory.from_score(score),
        breakdown=breakdown,
        forecast_window=window,
    )


class _FakeScorer:
    """Scorer that derives each step's score from its wave height (1 m = 1 pt)."""

    def score(
        self, step: ForecastStep, conditions: Any, daylight: DaylightInfo
    ) -> ScoreResult:  # pragma: no cover - not used by score_series path
        return _score_result(int(step.wave_height_m))

    def score_series(
        self,
        forecast: Forecast,
        conditions: Any,
        daylight_resolver: DaylightResolver | None = None,
    ) -> list[ScoreResult]:
        return [_score_result(int(step.wave_height_m)) for step in forecast.steps]


class _FakeActivity(Activity[Any]):
    """A registered activity whose scorer is the deterministic fake scorer."""

    key = _FAKE_ACTIVITY_KEY
    display_name = "Fake"
    available_in_mvp = True

    def scorer(self) -> Any:
        return _FakeScorer()

    def conditions_schema(self) -> type[ConditionsModel]:  # pragma: no cover
        return SurfConditions

    def default_forecast_provider_key(self) -> str:  # pragma: no cover
        return "fake"


class _FakeSubscriptionService:
    """Returns scripted subscriptions and a forecast target (no DB)."""

    def __init__(
        self,
        *,
        subscriptions: list[Subscription] | None = None,
        target: SubscriptionForecastTarget | None = None,
    ) -> None:
        self._subscriptions = subscriptions or []
        self._target = target

    async def list_for_user(self, user_id: int) -> list[Subscription]:
        return self._subscriptions

    async def get_forecast_target(
        self, subscription_id: int
    ) -> SubscriptionForecastTarget | None:
        return self._target


class _FakeSpotDiscovery:
    """Returns a fixed tuple of spots for any discovery query."""

    def __init__(self, spots: tuple[SurfSpot, ...]) -> None:
        self._spots = spots

    def discover(
        self, center: GeoPoint, radius_km: float, *, subscription_id: int | None = None
    ) -> Any:
        # Mirror SpotDiscoveryResult's shape used by StatusService.
        from brizocast.services.spot_discovery_service import SpotDiscoveryResult

        return SpotDiscoveryResult(
            center=center,
            radius_km=radius_km,
            spots=self._spots,
            subscription_id=subscription_id,
        )


class _FakeForecastService:
    """Returns a one-step forecast per spot, or fails for configured spots."""

    def __init__(
        self,
        *,
        waves: dict[str, float],
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self._waves = waves
        self._failing = failing
        self.calls: list[str] = []

    async def get_forecast(self, spot: SurfSpot, window: ForecastWindow) -> Forecast:
        self.calls.append(spot.spot_key)
        if spot.spot_key in self._failing:
            raise ProviderRequestError("boom", provider="fake")
        return Forecast(
            spot_id=spot.spot_key,
            steps=[
                ForecastStep(
                    timestamp=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
                    wave_height_m=self._waves[spot.spot_key],
                    swell_period_s=11.0,
                    swell_direction_deg=300.0,
                    wind_speed_kmh=8.0,
                    wind_direction_deg=90.0,
                )
            ],
        )


class _FakePresetService:
    """Returns a fixed set of effective conditions for any subscription."""

    def __init__(self) -> None:
        self._conditions = SurfConditions(
            min_wave_m=0.5,
            max_wave_m=3.0,
            min_period_s=8.0,
            max_wind_kmh=25.0,
        )

    async def resolve_effective_conditions(
        self, subscription: Subscription, *, region: str | None = None
    ) -> SurfConditions:
        return self._conditions


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    """Snapshot/restore the process-global registry around each test."""

    snapshot = dict(ActivityRegistry._items)
    ActivityRegistry.register(_FakeActivity())
    try:
        yield
    finally:
        ActivityRegistry._items.clear()
        ActivityRegistry._items.update(snapshot)


def _spot(spot_key: str, name: str) -> SurfSpot:
    return SurfSpot(spot_key=spot_key, name=name, lat=39.34, lon=-9.35)


def _target(*, muted: bool = False, snooze_until: datetime | None = None) -> SubscriptionForecastTarget:
    sub = Subscription(
        user_id=1,
        activity_id=1,
        location_id=1,
        search_radius_km=30.0,
        muted=muted,
        snooze_until=snooze_until,
    )
    return SubscriptionForecastTarget(
        subscription=sub,
        activity_key=_FAKE_ACTIVITY_KEY,
        center=GeoPoint(lat=39.34, lon=-9.35),
        location_label="Peniche",
        search_radius_km=30.0,
    )


def _build_service(
    *,
    subscription_service: _FakeSubscriptionService,
    spots: tuple[SurfSpot, ...] = (),
    forecast: _FakeForecastService | None = None,
    scheduler_runs: InMemorySchedulerState | None = None,
) -> StatusService:
    return StatusService(
        subscription_service,  # type: ignore[arg-type]
        _FakeSpotDiscovery(spots),  # type: ignore[arg-type]
        forecast or _FakeForecastService(waves={}),  # type: ignore[arg-type]
        _FakePresetService(),  # type: ignore[arg-type]
        scheduler_runs or InMemorySchedulerState(),
        now=lambda: datetime(2025, 6, 1, 5, 0, tzinfo=UTC),
    )


# --------------------------------------------------------------------------- #
# /status (Req 13.3)
# --------------------------------------------------------------------------- #
async def test_active_subscription_count_counts_only_active() -> None:
    subs = [
        Subscription(user_id=1, activity_id=1, location_id=1, active=True),
        Subscription(user_id=1, activity_id=1, location_id=2, active=False),
        Subscription(user_id=1, activity_id=1, location_id=3, active=True),
    ]
    service = _build_service(
        subscription_service=_FakeSubscriptionService(subscriptions=subs)
    )

    assert await service.active_subscription_count(1) == 2


async def test_last_scheduler_run_reads_shared_state() -> None:
    when = datetime(2025, 6, 21, 6, 30, tzinfo=UTC)
    state = InMemorySchedulerState(initial=when)
    service = _build_service(
        subscription_service=_FakeSubscriptionService(), scheduler_runs=state
    )

    assert await service.last_scheduler_run() == when


async def test_last_scheduler_run_is_none_until_recorded() -> None:
    state = InMemorySchedulerState()
    service = _build_service(
        subscription_service=_FakeSubscriptionService(), scheduler_runs=state
    )

    assert await service.last_scheduler_run() is None

    # The scheduler (task 8.3) records a successful run through the same state.
    recorded = datetime(2025, 6, 22, 7, 0, tzinfo=UTC)
    await state.record_success_async(recorded)
    assert await service.last_scheduler_run() == recorded


# --------------------------------------------------------------------------- #
# /forecast (Req 13.4)
# --------------------------------------------------------------------------- #
async def test_best_forecast_picks_highest_scoring_spot() -> None:
    spots = (_spot("pt/a", "Spot A"), _spot("pt/b", "Spot B"))
    forecast = _FakeForecastService(waves={"pt/a": 60.0, "pt/b": 88.0})
    service = _build_service(
        subscription_service=_FakeSubscriptionService(target=_target()),
        spots=spots,
        forecast=forecast,
    )

    best = await service.best_forecast_for_subscription(7)

    assert best.has_result is True
    assert best.spot is not None and best.spot.name == "Spot B"
    assert best.score == 88
    assert best.category == ScoreCategory.from_score(88)
    assert best.location_label == "Peniche"


async def test_best_forecast_ignores_mute_and_snooze() -> None:
    # A muted + actively-snoozed subscription still reports a forecast (Req 13.4).
    future = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    spots = (_spot("pt/a", "Spot A"),)
    forecast = _FakeForecastService(waves={"pt/a": 72.0})
    service = _build_service(
        subscription_service=_FakeSubscriptionService(
            target=_target(muted=True, snooze_until=future)
        ),
        spots=spots,
        forecast=forecast,
    )

    best = await service.best_forecast_for_subscription(7)

    assert best.has_result is True
    assert best.score == 72


async def test_best_forecast_no_nearby_spots() -> None:
    service = _build_service(
        subscription_service=_FakeSubscriptionService(target=_target()),
        spots=(),
    )

    best = await service.best_forecast_for_subscription(7)

    assert best.has_nearby_spots is False
    assert best.has_result is False
    assert best.spot is None and best.score is None
    assert best.location_label == "Peniche"


async def test_best_forecast_skips_failing_spots() -> None:
    spots = (_spot("pt/a", "Spot A"), _spot("pt/b", "Spot B"))
    forecast = _FakeForecastService(
        waves={"pt/b": 75.0}, failing=frozenset({"pt/a"})
    )
    service = _build_service(
        subscription_service=_FakeSubscriptionService(target=_target()),
        spots=spots,
        forecast=forecast,
    )

    best = await service.best_forecast_for_subscription(7)

    assert best.has_result is True
    assert best.spot is not None and best.spot.name == "Spot B"
    assert best.score == 75
    assert forecast.calls == ["pt/a", "pt/b"]  # both attempted; A failed


async def test_best_forecast_all_spots_fail_yields_no_result() -> None:
    spots = (_spot("pt/a", "Spot A"),)
    forecast = _FakeForecastService(waves={}, failing=frozenset({"pt/a"}))
    service = _build_service(
        subscription_service=_FakeSubscriptionService(target=_target()),
        spots=spots,
        forecast=forecast,
    )

    best = await service.best_forecast_for_subscription(7)

    assert best.has_nearby_spots is True
    assert best.has_result is False
    assert best.spot is None and best.score is None


async def test_best_forecast_unknown_subscription_raises() -> None:
    service = _build_service(
        subscription_service=_FakeSubscriptionService(target=None)
    )

    with pytest.raises(NotFoundError):
        await service.best_forecast_for_subscription(999)
