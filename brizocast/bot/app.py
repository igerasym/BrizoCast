"""Application bootstrap and composition root (task 11.1, Req 15.1, 16.4).

This module is the single entry point that assembles the whole BrizoCast
application and runs the Telegram bot. Together with
:class:`~brizocast.core.container.Container` it is the *only* place that knows
about every layer — the Clean-Architecture composition root — wiring concrete
adapters (Telegram, SQLAlchemy, providers, APScheduler) to the ports the rest of
the code depends on.

Run it as::

    python -m brizocast.bot.app

(the image's ``CMD``). It runs the bot via **long polling** — outbound calls to
Telegram only, with no inbound HTTP server, so no listening socket and no
inbound attack surface is opened. The bot token is read from validated
:class:`~brizocast.config.settings.Settings` (never hard-coded).

Startup sequence
----------------
:func:`main` builds the application and calls
:meth:`telegram.ext.Application.run_polling`, which owns the event loop. The
async, loop-bound steps run in the application's lifecycle hooks so they execute
on the same loop the bot and scheduler use:

#. ``configure_logging()`` then ``load_settings()`` — an invalid configuration is
   logged field-by-field and terminates startup with a non-zero exit (Req 15.1,
   15.3, 15.4).
#. Build the async engine and session factory, the DI :class:`Container` (with
   the session factory injected), and register the built-in activities.
#. Build the python-telegram-bot ``Application`` (long polling) and, over its
   ``Bot``, the :class:`~brizocast.notifications.sender.TelegramSender` wrapped
   in a :class:`~brizocast.notifications.sender.RetryingNotificationSender`.
#. Wire the forecast-check job, the shared digest buffer + digest jobs, the
   shared scheduler-run state, and the :class:`SchedulerRunner`; register every
   command/conversation handler (the unknown-command fallback **last**).
#. ``post_init`` (on the running loop): ``bootstrap_database`` (Req 16.4), seed
   the ``activities`` table and publish the ``{activity_key: activity_id}`` map
   into ``bot_data``, then start the scheduler.
#. ``post_shutdown``: stop the scheduler and dispose the engine for a clean exit.

Requirements covered: 15.1, 16.4 (operationally integrates 13.1, 13.7, 14.*).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, BaseHandler, ContextTypes

from brizocast.activities.bootstrap import register_builtin_activities
from brizocast.activities.registry import ActivityRegistry
from brizocast.bot.handlers.help import build_misc_handlers
from brizocast.bot.handlers.location import build_location_handlers
from brizocast.bot.handlers.presets import build_preset_handlers
from brizocast.bot.handlers.settings import build_settings_handlers
from brizocast.bot.handlers.start import build_start_handlers
from brizocast.bot.handlers.status import build_status_handlers
from brizocast.bot.handlers.subscriptions import (
    CTX_ACTIVITY_IDS,
    build_subscription_handlers,
)
from brizocast.bot.keyboards.menu import BOT_COMMANDS
from brizocast.config.settings import Settings, load_settings
from brizocast.config.overrides import ConfigOverrideStore, OverrideAwareSettings
from brizocast.core.container import (
    FEEDBACK_SERVICE_KEY,
    FORECAST_SERVICE_KEY,
    LOCATION_SERVICE_KEY,
    NOTIFICATION_ENGINE_KEY,
    NOTIFICATION_SERVICE_KEY,
    PLAN_EXPIRY_SERVICE_KEY,
    PRESET_SERVICE_KEY,
    SPOT_DISCOVERY_SERVICE_KEY,
    SUBSCRIPTION_SERVICE_KEY,
    USER_SERVICE_KEY,
    Container,
    SessionFactory,
)
from brizocast.core.logging import BoundLogger, configure_logging, get_logger
from brizocast.database.bootstrap import bootstrap_database
from brizocast.database.session import (
    create_engine,
    create_session_factory,
    session_scope,
)
from brizocast.models.activity import Activity as ActivityRow
from brizocast.models.admin_command import AdminCommand
from brizocast.models.user import User
from brizocast.notifications.engine import NotificationEngine
from brizocast.notifications.modes import DigestItem, NotificationMode
from brizocast.notifications.sender import (
    RetryingNotificationSender,
    SendRequest,
    TelegramSender,
)
from brizocast.repositories.json_spot_repo import ensure_spot_dataset_seeded
from brizocast.scheduler.digest_jobs import (
    DigestBuffer,
    DigestJobRunner,
    DigestTarget,
)
from brizocast.scheduler.forecast_check_job import (
    ForecastCheckJob,
    SessionUserChatIdResolver,
)
from brizocast.scheduler.runner import SchedulerRunner
from brizocast.services.admin_command_service import (
    AdminCommandService,
    AdminCommandType,
    CommandHandler,
)
from brizocast.services.feedback_service import FeedbackService
from brizocast.services.forecast_service import ForecastService
from brizocast.services.location_service import LocationService
from brizocast.services.notification_service import NotificationService
from brizocast.services.plan_expiry_service import PlanExpiryService
from brizocast.services.preset_service import PresetService
from brizocast.services.provider_selector import ProviderSelector
from brizocast.services.spot_discovery_service import SpotDiscoveryService
from brizocast.services.spot_admin_service import SpotAdminService
from brizocast.services.spot_ingestion_service import SpotIngestionService
from brizocast.providers.geocoding.reverse import NominatimReverseGeocoder
from brizocast.providers.spotcatalog.surfline import SurflineSpotCatalog
from brizocast.services.sqlite_scheduler_state import SqliteSchedulerState
from brizocast.services.status_service import StatusService
from brizocast.services.subscription_service import SubscriptionService
from brizocast.services.user_service import UserService

__all__ = ["build_application", "main"]

# A fully-parametrised PTB application; the default builder produces this shape.
# ``Any`` keeps the composition root free of PTB's six context type parameters.
BotApplication = Application[Any, Any, Any, Any, Any, Any]
# A bot handler bound to the default context type, registered on the app.
_Handler = BaseHandler[Any, ContextTypes.DEFAULT_TYPE, Any]

# The id and cadence of the periodic plan-expiry job (Req 20.7). A daily check
# is ample: a Paid plan flips to expired the first run after its expiry passes.
PLAN_EXPIRY_JOB_ID: Final = "plan-expiry"
PLAN_EXPIRY_INTERVAL_HOURS: Final = 24

# The id and cadence of the admin-command-drain job (Req 8.3, 9.3). A dedicated
# job (independent of the forecast-check job) drains the panel's command queue
# so a long forecast pass never delays command pickup.
ADMIN_COMMAND_DRAIN_JOB_ID: Final = "admin-command-drain"
ADMIN_COMMAND_DRAIN_INTERVAL_MINUTES: Final = 1

# The id and cadence of the log-level sync job. Reads the LOG_LEVEL override
# from the shared DB and applies it to the running bot's logger, so a level
# chosen in the admin panel takes effect within a minute without a restart.
LOG_LEVEL_SYNC_JOB_ID: Final = "log-level-sync"
LOG_LEVEL_SYNC_INTERVAL_MINUTES: Final = 1

# Private ``bot_data`` keys under which the bootstrap stashes the loop-bound
# runtime collaborators for the lifecycle hooks to pick up.
_RT_ENGINE: Final = "_brizocast_engine"
_RT_SESSION_FACTORY: Final = "_brizocast_session_factory"
_RT_SCHEDULER_RUNNER: Final = "_brizocast_scheduler_runner"
_RT_SPOT_DATASET_PATH: Final = "_brizocast_spot_dataset_path"


# --------------------------------------------------------------------------- #
# Composition-root adapters
# --------------------------------------------------------------------------- #
class _DigestBufferSink:
    """Adapts the synchronous :class:`DigestBuffer` to the async ``DigestSink``.

    The forecast-check job pushes a subscription's buffered digest items through
    the async :class:`~brizocast.scheduler.forecast_check_job.DigestSink` port;
    the shared :class:`DigestBuffer` exposes a synchronous ``append``. This thin
    adapter bridges the two so the *same* buffer instance the digest jobs drain
    receives what the forecast-check job buffers.
    """

    def __init__(self, buffer: DigestBuffer) -> None:
        self._buffer = buffer

    async def buffer(
        self, subscription_id: int, items: Sequence[DigestItem]
    ) -> None:
        """Append ``items`` to ``subscription_id``'s shared digest buffer."""
        self._buffer.append(subscription_id, items)


class _DigestSubscriptionSource:
    """Resolves a digest mode's delivery targets by joining subs to chat ids.

    Satisfies the
    :class:`~brizocast.scheduler.digest_jobs.DigestSubscriptionSource` port: for
    a digest mode it returns one :class:`DigestTarget` per active subscription in
    that mode whose owner has a resolvable Telegram chat id. Subscriptions whose
    owner cannot be resolved are skipped (they cannot be delivered to).
    """

    def __init__(
        self,
        subscription_service: SubscriptionService,
        chat_ids: SessionUserChatIdResolver,
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        self._subscriptions = subscription_service
        self._chat_ids = chat_ids
        self._log = logger or get_logger("digest_subscription_source")

    async def targets_for_mode(
        self, mode: NotificationMode
    ) -> Sequence[DigestTarget]:
        """Return the delivery targets for subscriptions in ``mode``."""
        targets: list[DigestTarget] = []
        for sub in await self._subscriptions.list_all_active():
            if sub.notification_mode != mode.value:
                continue
            chat_id = await self._chat_ids.chat_id_for_user(sub.user_id)
            if chat_id is None:
                self._log.warning(
                    "no chat id for user %s; skipping digest target for sub %s",
                    sub.user_id,
                    sub.id,
                )
                continue
            targets.append(DigestTarget(subscription_id=sub.id, chat_id=chat_id))
        return targets


# --------------------------------------------------------------------------- #
# Activity seeding
# --------------------------------------------------------------------------- #
async def _seed_activities(session_factory: SessionFactory) -> dict[str, int]:
    """Ensure every registered activity has a row and return the id map.

    Inserts a row (e.g. Surf) for any registered activity missing from the
    ``activities`` table, then returns the ``{activity_key: activity_id}`` map
    the subscription handlers read from ``bot_data`` (see
    :data:`~brizocast.bot.handlers.subscriptions.CTX_ACTIVITY_IDS`). Idempotent:
    existing rows are reused, so repeated startups never duplicate activities.
    """
    mapping: dict[str, int] = {}
    async with session_scope(session_factory) as session:
        for activity in ActivityRegistry.all():
            row = (
                await session.execute(
                    select(ActivityRow).where(ActivityRow.key == activity.key)
                )
            ).scalar_one_or_none()
            if row is None:
                row = ActivityRow(
                    key=activity.key,
                    display_name=activity.display_name,
                    available_in_mvp=activity.available_in_mvp,
                )
                session.add(row)
                await session.flush()
            mapping[activity.key] = row.id
    return mapping


# --------------------------------------------------------------------------- #
# Lifecycle hooks (run on the application's event loop)
# --------------------------------------------------------------------------- #
async def _post_init(application: BotApplication) -> None:
    """Bootstrap the database, seed activities, and start the scheduler.

    Runs on the bot's event loop (PTB invokes it after initialising the
    ``Application``), so the async engine and the ``AsyncIOScheduler`` are bound
    to the loop that serves the bot. Creates the schema if absent (Req 16.4),
    publishes the activity id map into ``bot_data`` (Req 3.1 wiring), and starts
    the periodic jobs.
    """
    log = get_logger("bot.app")
    engine: AsyncEngine = application.bot_data[_RT_ENGINE]
    session_factory: SessionFactory = application.bot_data[_RT_SESSION_FACTORY]
    runner: SchedulerRunner = application.bot_data[_RT_SCHEDULER_RUNNER]

    await bootstrap_database(engine)
    spot_dataset_path: str = application.bot_data[_RT_SPOT_DATASET_PATH]
    await ensure_spot_dataset_seeded(spot_dataset_path)
    application.bot_data[CTX_ACTIVITY_IDS] = await _seed_activities(session_factory)
    # Populate the native "≡ Menu" command list so users can navigate by tapping.
    await application.bot.set_my_commands(BOT_COMMANDS)
    runner.start()
    log.info("BrizoCast started: database ready, scheduler running, long polling")


async def _post_shutdown(application: BotApplication) -> None:
    """Stop the scheduler and dispose the engine for a clean shutdown."""
    log = get_logger("bot.app")
    runner: SchedulerRunner | None = application.bot_data.get(_RT_SCHEDULER_RUNNER)
    engine: AsyncEngine | None = application.bot_data.get(_RT_ENGINE)
    if runner is not None:
        runner.shutdown()
    if engine is not None:
        await engine.dispose()
    log.info("BrizoCast stopped: scheduler shut down, database engine disposed")


# --------------------------------------------------------------------------- #
# Composition root
# --------------------------------------------------------------------------- #
def build_application(settings: Settings) -> BotApplication:
    """Assemble the fully-wired Telegram application (without starting polling).

    This is the composition root proper: it builds the engine + session factory,
    the DI container, every application service, the notification sender, the
    forecast-check and digest jobs, the scheduler, and registers all command and
    conversation handlers — but does **not** start polling, the scheduler, or
    touch the network. It is therefore safe to call from a smoke test to inspect
    the registered handlers.

    Args:
        settings: The validated application configuration (token, database URL,
            intervals, …).

    Returns:
        A ready-to-run :class:`telegram.ext.Application`. Call
        :meth:`~telegram.ext.Application.run_polling` (see :func:`main`) to start
        it; the database bootstrap and scheduler start happen in ``post_init``.
    """
    log = get_logger("bot.app")

    # --- infrastructure: engine, session factory, DI container ---------- #
    engine = create_engine(settings.DATABASE_URL)
    session_factory = create_session_factory(engine)
    container = Container(settings, session_factory=session_factory)

    # Make the bundled activities discoverable before anything resolves them.
    register_builtin_activities()

    # --- application services (single shared instances via the container) #
    user_service = container.resolve(USER_SERVICE_KEY, UserService)
    location_service = container.resolve(LOCATION_SERVICE_KEY, LocationService)
    preset_service = container.resolve(PRESET_SERVICE_KEY, PresetService)
    feedback_service = container.resolve(FEEDBACK_SERVICE_KEY, FeedbackService)
    notification_service = container.resolve(
        NOTIFICATION_SERVICE_KEY, NotificationService
    )
    forecast_service = container.resolve(FORECAST_SERVICE_KEY, ForecastService)
    spot_discovery = container.resolve(
        SPOT_DISCOVERY_SERVICE_KEY, SpotDiscoveryService
    )
    subscription_service = container.resolve(
        SUBSCRIPTION_SERVICE_KEY, SubscriptionService
    )
    plan_expiry_service = container.resolve(
        PLAN_EXPIRY_SERVICE_KEY, PlanExpiryService
    )
    notification_engine = container.resolve(
        NOTIFICATION_ENGINE_KEY, NotificationEngine
    )

    # Override-aware settings over the shared config_overrides table, and the
    # per-tick forecast provider selector that reads it (Req 7.3). The admin
    # panel persists a FORECAST_PROVIDER override; the selector picks it up on
    # the next forecast-check tick without a restart.
    overrides = OverrideAwareSettings(settings, ConfigOverrideStore(session_factory))
    provider_selector = ProviderSelector(overrides, settings)

    # Spot ingestion: import named spots from the catalogue (Surfline) into our
    # shared dataset when a user sets a location. Optional + graceful — wired
    # only when enabled; a catalogue outage never blocks the location flow.
    spot_ingestion: SpotIngestionService | None = None
    if settings.SPOT_INGEST_ENABLED:
        spot_ingestion = SpotIngestionService(
            SurflineSpotCatalog(logger=get_logger("surfline_catalog")),
            SpotAdminService(settings.SPOT_DATASET_PATH),
            reverse_geocoder=NominatimReverseGeocoder(
                logger=get_logger("reverse_geocoder")
            ),
            preset_service=preset_service if settings.AI_ENABLED else None,
            logger=get_logger("spot_ingestion"),
        )

    # Shared scheduler-run state: the runner writes the completion time, the
    # StatusService reads it for /status (Req 13.3, 14.4). Persisted in the
    # shared DB (``scheduler_runs`` row id=1) so the admin panel — a separate
    # process — can read the last successful run for its stats page (Req 11.2).
    scheduler_state = SqliteSchedulerState(session_factory)
    status_service = StatusService(
        subscription_service,
        spot_discovery,
        forecast_service,
        preset_service,
        scheduler_state,
        logger=get_logger("status_service"),
    )

    # --- the Telegram application (long polling, outbound only) --------- #
    application: BotApplication = (
        ApplicationBuilder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Telegram delivery: the only place that knows about python-telegram-bot in
    # the notification stack, wrapped with the retry/digest-fallback policy.
    sender = RetryingNotificationSender(
        TelegramSender(application.bot, logger=get_logger("telegram_sender")),
        retry_count=settings.NOTIFY_RETRY_COUNT,
        logger=get_logger("notification_sender"),
    )

    # --- scheduler pipeline: forecast-check + digests + plan-expiry ----- #
    chat_id_resolver = SessionUserChatIdResolver(session_factory)
    digest_buffer = DigestBuffer()
    digest_runner = DigestJobRunner(
        buffer=digest_buffer,
        subscriptions=_DigestSubscriptionSource(
            subscription_service, chat_id_resolver
        ),
        sender=sender,
        logger=get_logger("digest_jobs"),
    )
    forecast_job = ForecastCheckJob(
        subscription_service,
        spot_discovery,
        forecast_service,
        preset_service,
        notification_service,
        notification_engine,
        sender,
        chat_id_resolver,
        digest_sink=_DigestBufferSink(digest_buffer),
        provider_selector=provider_selector,
        logger=get_logger("forecast_check_job"),
    )

    scheduler = AsyncIOScheduler()
    scheduler_runner = SchedulerRunner(
        forecast_job,
        digest_runner,
        scheduler_state,
        settings,
        scheduler=scheduler,
        logger=get_logger("scheduler_runner"),
    )
    # The periodic plan-expiry check shares the runner's scheduler (Req 20.7).
    scheduler.add_job(
        plan_expiry_service.run,
        IntervalTrigger(hours=PLAN_EXPIRY_INTERVAL_HOURS),
        id=PLAN_EXPIRY_JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # --- admin command-queue drain (Req 8.3, 9.3) ---------------------- #
    # A dedicated job drains the panel's admin_commands queue on its own short
    # interval, independent of the forecast-check job. Handlers map each command
    # type to a bot capability the panel cannot reach directly.
    admin_command_service = AdminCommandService(session_factory)

    async def _handle_run_forecast_check(_command: AdminCommand) -> None:
        """Run one forecast-check pass on demand (Req 8.3)."""
        await forecast_job.run_once()

    async def _handle_broadcast(command: AdminCommand) -> None:
        """Deliver the broadcast text to every known user (Req 9.3).

        Targets are every distinct ``User.telegram_user_id`` (which doubles as
        the chat id); delivery reuses the resilient batch sender so a single
        failed delivery is logged and the batch continues.
        """
        text = str(command.payload.get("text", "")).strip()
        if not text:
            # Empty text is rejected at the panel (Req 9.4); nothing to deliver.
            return
        async with session_scope(session_factory) as session:
            result = await session.execute(
                select(User.telegram_user_id).distinct()
            )
            chat_ids = [int(chat_id) for chat_id in result.scalars().all()]
        await sender.send_batch(
            SendRequest(chat_id=chat_id, text=text) for chat_id in chat_ids
        )

    drain_handlers: dict[AdminCommandType, CommandHandler] = {
        AdminCommandType.RUN_FORECAST_CHECK: _handle_run_forecast_check,
        AdminCommandType.BROADCAST: _handle_broadcast,
    }

    async def _drain_admin_commands() -> None:
        """Drain the admin command queue once (Req 8.3, 9.3, 12.4)."""
        await admin_command_service.drain(drain_handlers)

    scheduler.add_job(
        _drain_admin_commands,
        IntervalTrigger(minutes=ADMIN_COMMAND_DRAIN_INTERVAL_MINUTES),
        id=ADMIN_COMMAND_DRAIN_JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # --- log-level sync (apply admin panel's LOG_LEVEL live) ------------ #
    _log_sync_logger = get_logger("bot.log_level_sync")

    async def _sync_log_level() -> None:
        """Apply the LOG_LEVEL override chosen in the admin panel, if changed."""
        from brizocast.core.logging import get_log_level, set_log_level

        desired = await overrides.log_level()
        current = get_log_level()
        if desired != current:
            set_log_level(desired)
            _log_sync_logger.info("log level changed: %s -> %s", current, desired)

    scheduler.add_job(
        _sync_log_level,
        IntervalTrigger(minutes=LOG_LEVEL_SYNC_INTERVAL_MINUTES),
        id=LOG_LEVEL_SYNC_JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # --- health tracker: wire write-through callback to shared DB ----------- #
    from brizocast.services.health_tracker import tracker as _health_tracker

    _health_store = ConfigOverrideStore(session_factory)

    async def _persist_service_health(service: str, payload: dict[str, object]) -> None:
        """Write one service's health entry into config_overrides immediately."""
        # Read existing snapshot, update just this service, write back.
        current = await _health_store.get("SERVICE_HEALTH")
        snapshot: dict[str, object] = dict(current) if isinstance(current, dict) else {}
        snapshot[service] = payload
        await _health_store.set("SERVICE_HEALTH", snapshot)

    _health_tracker.set_persist_callback(_persist_service_health)

    # Global update logger in group -1: logs every incoming message/callback
    # without consuming it, so the Logs page shows user activity.
    _update_log = get_logger("bot.updates")

    async def _log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        username = f"@{user.username}" if user and user.username else str(user.id if user else "?")
        if update.message and update.message.text:
            _update_log.info("msg from %s: %s", username, update.message.text[:120])
        elif update.message and update.message.location:
            loc = update.message.location
            _update_log.info("location from %s: (%.4f, %.4f)", username, loc.latitude, loc.longitude)
        elif update.callback_query:
            _update_log.info("callback from %s: %s", username, update.callback_query.data)

    from telegram.ext import TypeHandler
    application.add_handler(TypeHandler(Update, _log_update), group=-1)

    feature_handlers: list[_Handler] = [
        *build_start_handlers(user_service),
        *build_location_handlers(
            location_service,
            user_service,
            spot_discovery=spot_discovery,
            spot_ingestion=spot_ingestion,
            ingest_radius_km=float(settings.SPOT_INGEST_RADIUS_KM),
        ),
        *build_subscription_handlers(
            subscription_service,
            location_service,
            user_service,
            status_service=status_service,
            preset_service=preset_service,
            spot_discovery=spot_discovery,
            spot_ingestion=spot_ingestion,
            ingest_radius_km=float(settings.SPOT_INGEST_RADIUS_KM),
        ),
        *build_preset_handlers(preset_service),
        *build_settings_handlers(subscription_service),
        *build_status_handlers(status_service, subscription_service),
    ]
    for handler in feature_handlers:
        application.add_handler(handler)
    # Misc handlers contribute /help, the feedback callbacks, and the
    # catch-all unknown-command fallback — the fallback is last in this list and
    # the whole list is registered after every per-feature handler (Req 13.7).
    for handler in build_misc_handlers(feedback_service):
        application.add_handler(handler)

    # Stash the loop-bound collaborators for the lifecycle hooks.
    application.bot_data[_RT_ENGINE] = engine
    application.bot_data[_RT_SESSION_FACTORY] = session_factory
    application.bot_data[_RT_SCHEDULER_RUNNER] = scheduler_runner
    application.bot_data[_RT_SPOT_DATASET_PATH] = settings.SPOT_DATASET_PATH

    log.info(
        "composition root assembled: %d feature handler(s) registered",
        len(feature_handlers),
    )
    return application


def main() -> None:
    """Configure logging, load settings, build the app, and run long polling.

    Terminates startup with a non-zero exit code when the configuration is
    missing or invalid — :func:`~brizocast.config.settings.load_settings` has
    already logged each offending field by name (Req 15.3, 15.4).
    """
    configure_logging()
    log = get_logger("bot.app")
    try:
        settings = load_settings()
    except ValidationError:
        # load_settings already logged the offending field(s) (Req 15.4).
        log.critical("startup aborted: invalid configuration")
        raise SystemExit(1) from None

    # Reconfigure logging with the level and file path from settings.
    configure_logging(level=settings.LOG_LEVEL, log_file=settings.LOG_FILE)

    application = build_application(settings)
    # python-telegram-bot v21's run_polling resolves the loop via
    # asyncio.get_event_loop(). On Python 3.13+ that no longer auto-creates a
    # loop in the main thread, so ensure one exists first (no-op on 3.12).
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    # run_polling owns the event loop and drives post_init / post_shutdown.
    application.run_polling()


if __name__ == "__main__":
    main()
