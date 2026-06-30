"""Status and on-demand forecast service (Req 13.3, 13.4; supports Property 28).

``StatusService`` backs the bot's ``/status`` and ``/forecast`` commands:

* :meth:`active_subscription_count` and :meth:`last_scheduler_run` answer
  ``/status`` — how many of a user's subscriptions are active and when the
  scheduler last completed a run (Req 13.3). The last-run time comes from the
  shared :class:`~brizocast.services.scheduler_state.SchedulerRunReader`, which
  is ``None`` ("never") until the scheduler completes its first run.
* :meth:`best_forecast_for_subscription` answers ``/forecast`` — it runs the same
  pipeline the scheduler will use (discover nearby spots → fetch forecasts →
  resolve effective conditions → score) for a single subscription and returns
  its current best surf score and spot **regardless of the subscription's mute
  or snooze state** (Req 13.4). Mute/snooze are notification-delivery guards; an
  on-demand forecast query deliberately ignores them.

The service is thin orchestration over the existing application services
(:class:`~brizocast.services.subscription_service.SubscriptionService`,
:class:`~brizocast.services.spot_discovery_service.SpotDiscoveryService`,
:class:`~brizocast.services.forecast_service.ForecastService`,
:class:`~brizocast.services.preset_service.PresetService`) and the
:class:`~brizocast.activities.registry.ActivityRegistry`. It owns no persistence
of its own; the clock and forecast horizon are injected so the result is
deterministic and unit-testable with fakes.

Requirements covered: 13.3, 13.4 (supports Property 28).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from brizocast.activities.registry import ActivityRegistry
from brizocast.core.domain.daylight import DaylightInfo, compute_daylight
from brizocast.core.domain.forecast import ForecastStep, ForecastWindow
from brizocast.core.domain.scoring import ScoreCategory, ScoreResult
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.errors import NotFoundError, ProviderRequestError
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.services.forecast_service import ForecastService
from brizocast.services.preset_service import PresetService
from brizocast.services.scheduler_state import SchedulerRunReader
from brizocast.services.spot_discovery_service import SpotDiscoveryService
from brizocast.services.subscription_service import SubscriptionService

__all__ = ["BestForecast", "DailyForecast", "PeriodBest", "StatusService"]

DEFAULT_FORECAST_WINDOW = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(UTC)


# Time periods for the daily forecast breakdown.
_MORNING = (time(6, 0), time(11, 0))
_MIDDAY = (time(11, 0), time(16, 0))
_EVENING = (time(16, 0), time(21, 0))


@dataclass(frozen=True, slots=True)
class BestForecast:
    subscription_id: int
    location_label: str
    has_nearby_spots: bool
    spot: SurfSpot | None
    score: int | None
    category: ScoreCategory | None

    @property
    def has_result(self) -> bool:
        return self.spot is not None and self.score is not None


@dataclass(frozen=True, slots=True)
class PeriodBest:
    """Best scoring result for a time-of-day period."""
    period_name: str       # "🌅 Morning", "☀️ Midday", "🌆 Evening"
    spot: SurfSpot | None
    score: int | None
    category: ScoreCategory | None
    step: ForecastStep | None
    result: ScoreResult | None


@dataclass(frozen=True)
class DailyForecast:
    """24-hour forecast split into morning / midday / evening best results."""
    subscription_id: int
    location_label: str
    has_nearby_spots: bool
    periods: list[PeriodBest] = field(default_factory=list)


class StatusService:
    """Reports status and the on-demand best forecast (Req 13.3, 13.4)."""

    def __init__(
        self,
        subscription_service: SubscriptionService,
        spot_discovery: SpotDiscoveryService,
        forecast_service: ForecastService,
        preset_service: PresetService,
        scheduler_runs: SchedulerRunReader,
        *,
        activity_registry: type[ActivityRegistry] = ActivityRegistry,
        now: Callable[[], datetime] = _utc_now,
        forecast_window: timedelta = DEFAULT_FORECAST_WINDOW,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            subscription_service: Source of the user's subscriptions and the
                per-subscription forecast target (location, radius, activity).
            spot_discovery: Discovers nearby surf spots within a radius.
            forecast_service: Supplies per-spot forecasts (TTL-cached).
            preset_service: Resolves a subscription's effective conditions.
            scheduler_runs: Read side of the shared scheduler-run state; supplies
                the most recent successful scheduler-run time for ``/status``.
            activity_registry: The activity registry used to resolve a
                subscription's scorer; injectable for testing.
            now: Clock returning the current time; injected for testability.
            forecast_window: Look-ahead window for the on-demand forecast query.
            logger: Optional bound logger; one is created when omitted.
        """
        self._subscriptions = subscription_service
        self._spots = spot_discovery
        self._forecast = forecast_service
        self._presets = preset_service
        self._scheduler_runs = scheduler_runs
        self._registry = activity_registry
        self._now = now
        self._window = forecast_window
        self._log = logger or get_logger(__name__)

    # -- /status -------------------------------------------------------- #

    async def active_subscription_count(self, user_id: int) -> int:
        """Return how many of ``user_id``'s subscriptions are active (Req 13.3)."""
        subscriptions = await self._subscriptions.list_for_user(user_id)
        return sum(1 for sub in subscriptions if sub.active)

    async def last_scheduler_run(self) -> datetime | None:
        """Return the most recent successful scheduler-run time, or ``None`` (Req 13.3).

        ``None`` means the scheduler has not completed a run yet (rendered as
        "never" by the ``/status`` formatter).
        """
        return await self._scheduler_runs.last_successful_run_async()

    # -- /forecast ------------------------------------------------------ #

    async def best_forecast_for_subscription(
        self, subscription_id: int
    ) -> BestForecast:
        """Return the current best score and spot for a subscription (Req 13.4).

        Runs the same pipeline the scheduler uses — discover nearby spots, fetch
        each spot's forecast, resolve the subscription's effective conditions,
        and score every forecast step — then returns the single best-scoring spot
        and its score. The computation **ignores the subscription's mute and
        snooze state** (Req 13.4): those guard notification delivery, not an
        explicit on-demand query.

        A spot whose forecast fetch fails is logged and skipped so one failing
        provider call does not sink the whole query (consistent with the
        scheduler's per-spot resilience, Req 6.5).

        Args:
            subscription_id: The subscription to evaluate.

        Returns:
            A :class:`BestForecast`; see its docstring for the no-spots and
            no-usable-forecast cases.

        Raises:
            NotFoundError: If no subscription with that id exists.
        """
        target = await self._subscriptions.get_forecast_target(subscription_id)
        if target is None:
            raise NotFoundError(f"subscription {subscription_id} does not exist")

        discovery = self._spots.discover(
            target.center,
            target.search_radius_km,
            subscription_id=subscription_id,
        )
        if not discovery.has_nearby_spots:
            # Req 5.5 / 13.4 — no spots within range; nothing to score.
            return BestForecast(
                subscription_id=subscription_id,
                location_label=target.location_label,
                has_nearby_spots=False,
                spot=None,
                score=None,
                category=None,
            )

        scorer = self._registry.get(target.activity_key).scorer()
        window = ForecastWindow(start=self._now(), end=self._now() + self._window)

        best_spot: SurfSpot | None = None
        best_score = -1
        best_category: ScoreCategory | None = None

        for spot in discovery.spots:
            try:
                forecast = await self._forecast.get_forecast(spot, window)
            except ProviderRequestError as exc:
                # Skip this spot for the query and keep going (Req 6.5).
                self._log.warning(
                    "forecast fetch failed for spot %s; skipping (%s)",
                    spot.spot_key,
                    exc,
                )
                continue

            # The region fallback for effective conditions comes from the spot
            # being scored, matching the scheduler's per-spot resolution.
            conditions = await self._presets.resolve_effective_conditions(
                target.subscription, region=spot.region
            )
            resolver = _daylight_resolver(spot)
            for result in scorer.score_series(forecast, conditions, resolver):
                if result.score > best_score:
                    best_score = result.score
                    best_spot = spot
                    best_category = result.category

        if best_spot is None:
            # Spots existed but none yielded a usable forecast step.
            return BestForecast(
                subscription_id=subscription_id,
                location_label=target.location_label,
                has_nearby_spots=True,
                spot=None,
                score=None,
                category=None,
            )

        return BestForecast(
            subscription_id=subscription_id,
            location_label=target.location_label,
            has_nearby_spots=True,
            spot=best_spot,
            score=best_score,
            category=best_category,
        )

    async def daily_forecast_for_subscription(
        self, subscription_id: int
    ) -> DailyForecast:
        """Return a 24h forecast split into morning/midday/evening periods.

        Each period contains the best-scoring spot and step in that time window.
        Used for the /forecast command in the subscription detail view.
        """
        target = await self._subscriptions.get_forecast_target(subscription_id)
        if target is None:
            raise NotFoundError(f"subscription {subscription_id} does not exist")

        discovery = self._spots.discover(
            target.center, target.search_radius_km, subscription_id=subscription_id
        )
        if not discovery.has_nearby_spots:
            return DailyForecast(
                subscription_id=subscription_id,
                location_label=target.location_label,
                has_nearby_spots=False,
            )

        scorer = self._registry.get(target.activity_key).scorer()
        now = self._now()
        window = ForecastWindow(start=now, end=now + self._window)

        # period name → (start_h, end_h) in UTC
        period_defs = [
            ("🌅 Morning", 6, 11),
            ("☀️ Midday", 11, 16),
            ("🌆 Evening", 16, 21),
        ]
        # Collect best per period
        bests: dict[str, tuple[SurfSpot, ScoreResult, ForecastStep] | None] = {
            name: None for name, _, _ in period_defs
        }

        for spot in discovery.spots:
            try:
                forecast = await self._forecast.get_forecast(spot, window)
            except ProviderRequestError as exc:
                self._log.warning("forecast fetch failed for %s: %s", spot.spot_key, exc)
                continue

            conditions = await self._presets.resolve_effective_conditions(
                target.subscription, region=spot.region
            )
            resolver = _daylight_resolver(spot)
            for step, result in zip(
                forecast.steps,
                scorer.score_series(forecast, conditions, resolver),
                strict=True,
            ):
                step_hour = result.forecast_window.start.astimezone(UTC).hour
                for name, h_start, h_end in period_defs:
                    if h_start <= step_hour < h_end:
                        current = bests[name]
                        if current is None or result.score > current[1].score:
                            bests[name] = (spot, result, step)
                        break

        periods: list[PeriodBest] = []
        for name, _, _ in period_defs:
            entry = bests[name]
            if entry:
                spot_b, result_b, step_b = entry
                periods.append(PeriodBest(
                    period_name=name,
                    spot=spot_b,
                    score=result_b.score,
                    category=result_b.category,
                    step=step_b,
                    result=result_b,
                ))
            else:
                periods.append(PeriodBest(
                    period_name=name,
                    spot=None, score=None, category=None, step=None, result=None,
                ))

        return DailyForecast(
            subscription_id=subscription_id,
            location_label=target.location_label,
            has_nearby_spots=True,
            periods=periods,
        )


def _daylight_resolver(spot: SurfSpot) -> Callable[[datetime], DaylightInfo]:
    """Build a daylight resolver for ``spot`` at its coordinates.

    Returns a callable mapping a step timestamp to the :class:`DaylightInfo` for
    that day at the spot, so the scorer can apply a subscription's daylight-only
    gate without the (coordinate-free) forecast carrying location data.
    """
    point = spot.point()
    return lambda timestamp: compute_daylight(point, timestamp.date())
