"""The periodic forecast-check job pipeline (Req 14.2, supports Property 24).

:class:`ForecastCheckJob` runs **one** forecast-check pass over every active
subscription. It is the orchestration the design's *Scheduler Forecast-Check
Flow* describes, wiring together the application services and the notification
engine without owning any business rule itself:

#. log the job start (Req 18.1);
#. load **all active** subscriptions (Req 14.2);
#. for each subscription, in isolation (Req 14.3, Property 24):

   * resolve its discovery target and select its activity's
     :class:`~brizocast.core.ports.scorer.Scorer` via the
     :class:`~brizocast.activities.registry.ActivityRegistry` by ``activity_key``
     (Req 17.6) — so a newly-registered activity flows through the *same* job
     (Req 17.5);
   * discover nearby spots; when none are within the radius, record no-spots and
     skip forecast collection for that subscription (Req 5.5);
   * fetch each spot's forecast through
     :class:`~brizocast.services.forecast_service.ForecastService` (TTL-cached);
     a :class:`~brizocast.core.errors.ProviderRequestError` is logged with
     provider/spot context and **only that spot** is skipped for this run
     (Req 6.5, 18.2);
   * resolve the subscription's effective conditions, score every forecast step,
     and reduce to the best candidate per spot/window;
   * run the :class:`~brizocast.notifications.engine.NotificationEngine` to get a
     :class:`~brizocast.notifications.engine.NotificationPlan` (anti-spam +
     mute/snooze/quiet-hours gating), dispatch the immediate alerts through the
     :class:`~brizocast.notifications.sender.RetryingNotificationSender`, and
     persist a ``Notification_Record`` for each delivered alert (Req 9.2);
   * buffer digest items for the digest jobs (task 8.5 — see *Digest hand-off*);

#. log job completion (Req 18.1) and return a :class:`ForecastCheckResult`.

Scope boundary
--------------
This module implements **only the job pipeline**. It deliberately does *not*
implement the APScheduler runner, the interval guard, or the completion-timestamp
semantics (task 8.3) — the runner calls :meth:`ForecastCheckJob.run_once` and
decides, from its normal return versus a raised exception, whether to record the
run timestamp (Req 14.4, 14.5). It also does not implement the digest jobs (task
8.5); it only *produces* the buffered items they consume.

Per-subscription error isolation (Req 14.3, Property 24)
--------------------------------------------------------
Each subscription is processed inside its own ``try``/``except``: any exception
raised while processing one subscription is logged and the loop **continues**
with the remaining subscriptions. A provider failure for a single spot is
narrower still — it is caught inside the per-spot loop so the other spots of the
same subscription are still evaluated (Req 6.5).

Chat-id lookup
--------------
A subscription stores its owner's internal ``user_id``; Telegram delivery needs
the user's ``telegram_user_id`` (which doubles as the chat id). The job resolves
it through the injected :class:`ChatIdResolver` port. The default production
implementation, :class:`SessionUserChatIdResolver`, reads the user row through
the user repository; tests inject a trivial in-memory mapping. When a chat id
cannot be resolved the subscription is skipped (it cannot be delivered to) and
the skip is logged.

Digest hand-off (to task 8.5)
-----------------------------
Items destined for a subscription's next digest — both digest-mode qualifying
scores and immediate alerts that exhausted their delivery retries (Req 10.4) —
are handed off **two** ways so the digest jobs can consume them however they are
wired:

* **Returned** on the :class:`ForecastCheckResult` as ``digest_buffered`` (a
  ``{subscription_id: (DigestItem, ...)}`` map), which makes the pass fully
  observable and unit-testable; and
* **pushed** to the optional injected :class:`DigestSink` (when one is provided),
  the shared buffer the digest jobs drain at their own triggers. Persisting or
  recording digest items is intentionally left to the digest job.

Requirements covered: 5.5, 6.5, 14.2, 14.3, 17.5, 17.6, 18.1, 18.2 (supports
Property 24).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import InlineKeyboardMarkup

from brizocast.activities.registry import ActivityRegistry
from brizocast.bot.formatters.alerts import build_alert_message
from brizocast.core.domain.daylight import DaylightInfo, compute_daylight
from brizocast.core.domain.forecast import ForecastStep, ForecastWindow
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.errors import ProviderRequestError
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.database.session import session_scope
from brizocast.notifications.engine import ImmediateDispatch, NotificationEngine
from brizocast.notifications.modes import DigestItem
from brizocast.notifications.sender import (
    InlineButton,
    InlineKeyboard,
    RetryingNotificationSender,
    SendRequest,
)
from brizocast.notifications.window import window_key
from brizocast.repositories.user_repo import SqlAlchemyUserRepository
from brizocast.services.forecast_service import ForecastService
from brizocast.services.notification_service import NotificationService
from brizocast.services.preset_service import PresetService
from brizocast.services.provider_selector import ProviderSelector
from brizocast.services.spot_discovery_service import SpotDiscoveryService
from brizocast.services.subscription_service import (
    SubscriptionForecastTarget,
    SubscriptionService,
)

__all__ = [
    "ChatIdResolver",
    "DigestSink",
    "ForecastCheckJob",
    "ForecastCheckResult",
    "SessionUserChatIdResolver",
]

# Default look-ahead horizon for the forecast window fetched per spot. A week
# covers the morning/evening *and* weekly-best-day digest needs; the actual
# number of steps returned is the provider's concern.
DEFAULT_FORECAST_HORIZON = timedelta(days=7)


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Collaborator ports
# --------------------------------------------------------------------------- #
@runtime_checkable
class ChatIdResolver(Protocol):
    """Resolves a subscription owner's internal id to a Telegram chat id.

    A subscription carries only the internal ``user_id``; delivery needs the
    user's ``telegram_user_id``. The job depends on this narrow port so it never
    imports the ORM directly and can be exercised with an in-memory fake. The
    production implementation is :class:`SessionUserChatIdResolver`.
    """

    async def chat_id_for_user(self, user_id: int) -> int | None:
        """Return the Telegram chat id for ``user_id``, or ``None`` if unknown."""
        ...


@runtime_checkable
class DigestSink(Protocol):
    """Receiver of items buffered for a subscription's next digest.

    The shared buffer the digest jobs (task 8.5) drain at their morning /
    evening / weekly triggers. The forecast-check job pushes a subscription's
    qualifying digest-mode scores (and any immediate alert that exhausted its
    delivery retries, Req 10.4) here; persisting them is the digest job's
    responsibility.
    """

    async def buffer(
        self, subscription_id: int, items: Sequence[DigestItem]
    ) -> None:
        """Append ``items`` to ``subscription_id``'s digest buffer."""
        ...


class SessionUserChatIdResolver:
    """Production :class:`ChatIdResolver` backed by the user repository.

    Reads the user row for an internal ``user_id`` within a short unit of work
    and returns its ``telegram_user_id`` (the chat id). Wired at the composition
    root (task 11.1) with the application's session factory.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Initialise the resolver.

        Args:
            session_factory: Async session maker providing the unit-of-work
                boundary for the user lookup.
        """
        self._session_factory = session_factory

    async def chat_id_for_user(self, user_id: int) -> int | None:
        """Return the user's ``telegram_user_id`` for ``user_id``, or ``None``."""
        async with session_scope(self._session_factory) as session:
            user = await SqlAlchemyUserRepository(session).get(user_id)
            return user.telegram_user_id if user is not None else None


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ForecastCheckResult:
    """Summary of one forecast-check pass (returned by :meth:`run_once`).

    Attributes:
        subscriptions_total: Active subscriptions loaded for the run (Req 14.2).
        subscriptions_processed: Subscriptions whose pipeline ran to completion
            (including the no-nearby-spots outcome, Req 5.5).
        subscriptions_skipped: Subscriptions skipped before processing because no
            forecast target or chat id could be resolved.
        subscriptions_failed: Subscriptions whose processing raised and was
            isolated so the run continued (Req 14.3).
        immediate_dispatched: Immediate alerts delivered and recorded (Req 9.2).
        immediate_failed: Immediate alerts whose delivery exhausted its retries
            and were deferred to the next digest (Req 10.4).
        digest_buffered: Items handed off per subscription for the digest jobs
            (the returned half of the digest hand-off).
    """

    subscriptions_total: int = 0
    subscriptions_processed: int = 0
    subscriptions_skipped: int = 0
    subscriptions_failed: int = 0
    immediate_dispatched: int = 0
    immediate_failed: int = 0
    digest_buffered: dict[int, tuple[DigestItem, ...]] = field(default_factory=dict)


@dataclass
class _SubscriptionOutcome:
    """Mutable per-subscription tally accumulated while processing one sub."""

    dispatched: int = 0
    failed: int = 0
    digest_items: tuple[DigestItem, ...] = ()


def _daylight_resolver(spot: SurfSpot) -> Callable[[datetime], DaylightInfo]:
    """Build a daylight resolver for ``spot`` at its coordinates.

    Returns a callable mapping a step timestamp to the :class:`DaylightInfo` for
    that day at the spot, so the scorer can apply a subscription's daylight-only
    gate without the (coordinate-free) forecast carrying location data.
    """
    point = spot.point()
    return lambda timestamp: compute_daylight(point, timestamp.date())


def _to_neutral_keyboard(markup: InlineKeyboardMarkup) -> InlineKeyboard:
    """Convert a Telegram ``InlineKeyboardMarkup`` to the sender's neutral form.

    :func:`~brizocast.bot.formatters.alerts.build_alert_message` returns a
    Telegram markup, while
    :class:`~brizocast.notifications.sender.RetryingNotificationSender` accepts
    the framework-neutral :class:`~brizocast.notifications.sender.InlineKeyboard`
    (so the notification stack stays Telegram-agnostic). This bridges the two by
    reading each button's visible text and callback payload.
    """
    return tuple(
        tuple(
            InlineButton(
                text=button.text,
                # Telegram types ``callback_data`` as ``object``; alert buttons
                # always carry the feedback string payload (see build_alert_message).
                callback_data=button.callback_data
                if isinstance(button.callback_data, str)
                else "",
            )
            for button in row
        )
        for row in markup.inline_keyboard
    )


class ForecastCheckJob:
    """Runs one forecast-check pass over all active subscriptions (Req 14.2)."""

    def __init__(
        self,
        subscription_service: SubscriptionService,
        spot_discovery: SpotDiscoveryService,
        forecast_service: ForecastService,
        preset_service: PresetService,
        notification_service: NotificationService,
        engine: NotificationEngine,
        sender: RetryingNotificationSender,
        chat_ids: ChatIdResolver,
        *,
        activity_registry: type[ActivityRegistry] = ActivityRegistry,
        digest_sink: DigestSink | None = None,
        provider_selector: ProviderSelector | None = None,
        now: Callable[[], datetime] = _utc_now,
        forecast_horizon: timedelta = DEFAULT_FORECAST_HORIZON,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the job with its collaborators.

        Args:
            subscription_service: Loads active subscriptions and resolves each
                one's forecast target (center, radius, activity, label).
            spot_discovery: Discovers nearby surf spots within a radius (Req 5.*).
            forecast_service: Supplies per-spot forecasts (TTL-cached, Req 7.*).
            preset_service: Resolves a subscription's effective conditions.
            notification_service: Persists a ``Notification_Record`` per
                delivered alert (Req 9.2) and backs the engine's history lookup.
            engine: Applies anti-spam and mute/snooze/quiet-hours gating and
                routes results into immediate dispatches and digest buffers.
            sender: Resilient sender that retries delivery and reports per-item
                outcomes (Req 10.4, 18.3).
            chat_ids: Resolves a subscription owner's Telegram chat id.
            activity_registry: Registry used to select a subscription's scorer
                by activity key (Req 17.6); injectable for testing.
            digest_sink: Optional shared buffer the digest jobs drain (task 8.5);
                when ``None`` digest items are only returned on the result.
            now: Clock returning the current time; injected for testability.
            forecast_horizon: Look-ahead span of the forecast window fetched per
                spot.
            logger: Optional bound logger; one is created when omitted.
        """
        self._subscriptions = subscription_service
        self._spots = spot_discovery
        self._forecast = forecast_service
        self._presets = preset_service
        self._notifications = notification_service
        self._engine = engine
        self._sender = sender
        self._chat_ids = chat_ids
        self._registry = activity_registry
        self._digest_sink = digest_sink
        self._provider_selector = provider_selector
        self._now = now
        self._horizon = forecast_horizon
        self._log = logger or get_logger(__name__)

    async def run_once(self) -> ForecastCheckResult:
        """Run a single forecast-check pass over all active subscriptions.

        Logs the job start and completion (Req 18.1), loads every active
        subscription (Req 14.2), and processes each in isolation so a failure in
        one never stops the rest (Req 14.3, Property 24). Returns a
        :class:`ForecastCheckResult` summarising the pass, including the digest
        items buffered per subscription.

        The method returns normally on a completed pass; if loading the active
        subscriptions itself raises, the exception propagates so the runner
        (task 8.3) leaves the last-run timestamp unchanged (Req 14.5).
        """
        self._log.info("forecast-check job started")
        now = self._now()

        # Req 7.3 — resolve the live forecast provider for this tick so a
        # provider switch made in the admin panel applies without a restart.
        if self._provider_selector is not None:
            self._forecast.set_provider(await self._provider_selector.current())

        active = await self._subscriptions.list_all_active()
        self._log.info("loaded %d active subscription(s)", len(active))

        processed = 0
        skipped = 0
        failed = 0
        dispatched = 0
        immediate_failed = 0
        digest_buffered: dict[int, tuple[DigestItem, ...]] = {}

        for sub in active:
            sub_log = self._log.bind(subscription_id=sub.id)
            try:
                target = await self._subscriptions.get_forecast_target(sub.id)
                if target is None:
                    # Disappeared between listing and resolution; nothing to do.
                    sub_log.warning("subscription vanished before processing; skipping")
                    skipped += 1
                    continue

                chat_id = await self._chat_ids.chat_id_for_user(sub.user_id)
                if chat_id is None:
                    sub_log.warning(
                        "no Telegram chat id for user %s; skipping subscription",
                        sub.user_id,
                    )
                    skipped += 1
                    continue

                outcome = await self._process_subscription(target, chat_id, now)
                processed += 1
                dispatched += outcome.dispatched
                immediate_failed += outcome.failed
                if outcome.digest_items:
                    digest_buffered[sub.id] = outcome.digest_items
            except Exception:  # noqa: BLE001 - one bad subscription must not abort the run.
                # Req 14.3 / Property 24 — isolate the failure and keep going.
                failed += 1
                sub_log.exception("error processing subscription; continuing with the rest")

        result = ForecastCheckResult(
            subscriptions_total=len(active),
            subscriptions_processed=processed,
            subscriptions_skipped=skipped,
            subscriptions_failed=failed,
            immediate_dispatched=dispatched,
            immediate_failed=immediate_failed,
            digest_buffered=digest_buffered,
        )
        self._log.info(
            "forecast-check job completed: %d processed, %d skipped, %d failed, "
            "%d alert(s) dispatched, %d deferred to digest",
            processed,
            skipped,
            failed,
            dispatched,
            immediate_failed,
        )
        return result

    async def _process_subscription(
        self,
        target: SubscriptionForecastTarget,
        chat_id: int,
        now: datetime,
    ) -> _SubscriptionOutcome:
        """Run the full pipeline for one subscription and dispatch its alerts.

        Selects the scorer by activity (Req 17.6), discovers spots (skipping when
        none are nearby, Req 5.5), fetches and scores forecasts (skipping a spot
        on a provider error, Req 6.5), runs the notification engine, dispatches
        immediate alerts and records them (Req 9.2), and collects the digest
        buffer (Req 10.4, 10.5-10.7).
        """
        sub = target.subscription
        sub_log = self._log.bind(subscription_id=sub.id)

        # Load custom alert thresholds (min_alert_score, min_energy) if set.
        custom_min_score: int | None = None
        custom_min_energy: float | None = None
        preset_min_score: int | None = None

        from brizocast.database.session import session_scope
        from brizocast.repositories.condition_repo import SqlAlchemyCustomConditionRepository
        async with session_scope(self._presets._session_factory) as _sess:
            _repo = SqlAlchemyCustomConditionRepository(_sess)
            _custom = await _repo.get_for_subscription(sub.id)
            if _custom is not None:
                custom_min_score = getattr(_custom, "min_alert_score", None)
                custom_min_energy = getattr(_custom, "min_energy_kw", None)

        # Get min_alert_score from the subscription's preset (regional default).
        if sub.preset_id is not None:
            from brizocast.repositories.preset_repo import SqlAlchemyPresetRepository
            async with session_scope(self._presets._session_factory) as _sess2:
                _pr = SqlAlchemyPresetRepository(_sess2)
                _preset = await _pr.get(sub.preset_id)
                if _preset is not None:
                    preset_min_score = getattr(_preset, "min_alert_score", None)

        # Effective threshold: custom overrides preset overrides system default (40).
        effective_min_score: int = custom_min_score or preset_min_score or 40

        # Req 5.5 — discover nearby spots; skip forecast collection when none.
        discovery = self._spots.discover(
            target.center, target.search_radius_km, subscription_id=sub.id
        )
        if not discovery.has_nearby_spots:
            return _SubscriptionOutcome()

        # Req 17.6 — select the scorer for this subscription's activity.
        scorer = self._registry.get(target.activity_key).scorer()
        window = ForecastWindow(start=now, end=now + self._horizon)

        # Best candidate per (spot, forecast window), with the step and conditions.
        best_item: dict[tuple[str, str], DigestItem] = {}
        best_step: dict[tuple[str, str], ForecastStep] = {}
        best_offshore: dict[tuple[str, str], float | None] = {}  # offshore dir per key

        for spot in discovery.spots:
            try:
                forecast = await self._forecast.get_forecast(spot, window)
            except ProviderRequestError as exc:
                # Req 6.5 / 18.2 — log with provider/spot context and skip this
                # spot only; the rest of the subscription's spots continue.
                sub_log.warning(
                    "forecast provider request failed for spot %s (provider=%s); "
                    "skipping this spot",
                    spot.spot_key,
                    exc.provider,
                )
                continue

            conditions = await self._presets.resolve_effective_conditions(
                sub, region=spot.region
            )
            resolver = _daylight_resolver(spot)
            for step, result in zip(
                forecast.steps,
                scorer.score_series(forecast, conditions, resolver),
                strict=True,
            ):
                # Apply alert thresholds: custom > preset > system default.
                if result.score < effective_min_score:
                    continue
                if custom_min_energy is not None:
                    energy = 0.5 * step.wave_height_m ** 2 * step.swell_period_s
                    if energy < custom_min_energy:
                        continue
                key = (spot.spot_key, window_key(result.forecast_window))
                current = best_item.get(key)
                if current is None or result.score > current.score_result.score:
                    best_item[key] = DigestItem(spot=spot, score_result=result)
                    best_step[key] = step
                    best_offshore[key] = conditions.preferred_wind_dir_deg

        candidates = list(best_item.values())
        plan = await self._engine.process(sub, candidates, now, chat_id=chat_id)

        # Immediate alerts: format, dispatch with retry, record the delivered.
        # Limit to the single best immediate dispatch per run to avoid spam when
        # many forecast windows qualify simultaneously (e.g. on first run).
        digest_items: list[DigestItem] = list(plan.digest)
        dispatched = 0
        failed = 0
        if plan.immediate:
            top_dispatch = max(plan.immediate, key=lambda d: d.score_result.score)
            requests, dispatches = await self._build_requests((top_dispatch,), best_step, best_offshore)
            results = await self._sender.send_batch(requests)
            for dispatch, send_result in zip(dispatches, results, strict=True):
                if send_result.delivered:
                    # Req 9.2 — persist the record only after a successful send.
                    await self._notifications.record_sent(
                        dispatch.subscription_id,
                        dispatch.spot_key,
                        dispatch.score_result,
                        sent_at=now,
                    )
                    dispatched += 1
                else:
                    # Req 10.4 — retries exhausted: defer the alert to the digest.
                    digest_items.append(dispatch.item)
                    failed += 1

        buffered = tuple(digest_items)
        if buffered and self._digest_sink is not None:
            await self._digest_sink.buffer(sub.id, buffered)

        return _SubscriptionOutcome(
            dispatched=dispatched, failed=failed, digest_items=buffered
        )

    async def _build_requests(
        self,
        immediate: Sequence[ImmediateDispatch],
        best_step: dict[tuple[str, str], ForecastStep],
        best_offshore: dict[tuple[str, str], float | None] | None = None,
    ) -> tuple[list[SendRequest], list[ImmediateDispatch]]:
        """Build the send requests, including a static map image for each spot."""
        from brizocast.providers.maps.static_map import render_spot_map

        requests: list[SendRequest] = []
        dispatches: list[ImmediateDispatch] = []
        for dispatch in immediate:
            key = (
                dispatch.item.spot.spot_key,
                window_key(dispatch.item.score_result.forecast_window),
            )
            step = best_step[key]
            offshore = (best_offshore or {}).get(key)
            text, markup = build_alert_message(
                dispatch.item.spot,
                dispatch.item.score_result,
                step,
                dispatch.subscription_id,
                offshore_dir_deg=offshore,
            )
            # Generate static map image; gracefully falls back to text-only.
            spot = dispatch.item.spot
            wave_text = f"{step.wave_height_m:.1f}m · {step.swell_period_s:.0f}s · {step.wind_speed_kmh:.0f} km/h"
            map_buf = render_spot_map(
                spot.lat,
                spot.lon,
                spot_name=spot.name,
                score=dispatch.item.score_result.score,
                wave_text=wave_text,
            )
            photo_bytes = map_buf.read() if map_buf is not None else None

            requests.append(
                SendRequest(
                    chat_id=dispatch.chat_id,
                    text=text,
                    keyboard=_to_neutral_keyboard(markup),
                    photo_bytes=photo_bytes,
                    ref=dispatch,
                )
            )
            dispatches.append(dispatch)
        return requests, dispatches
