"""FastAPI dependency-injection wiring for the admin panel.

The panel runs as a *separate process* from the bot and owns its **own**
:class:`~brizocast.core.container.Container` pointed at the shared SQLite
database (Req 12.5, 13.4). This module both builds/caches that container and
exposes the thin ``Depends`` providers the routers inject — each reading the
live ``request.app.state`` set by the app factory (task 4.6) and resolving the
appropriate reused service / repository from the container.

Two kinds of provider live here:

* **Reused components** — resolved straight from the container by their stable
  registration key (``UserService``, ``SubscriptionService``, ``PresetService``,
  ``FeedbackService``, the forecast-cache repository, the spot repository), so
  the panel duplicates none of the bot's business logic (Req 12.5).
* **New admin services** — the panel-only use cases that wrap the shared session
  factory / override store (:class:`ConfigAdminService`,
  :class:`AdminCommandService`, :class:`PresetAdminService`,
  :class:`SqliteSchedulerState`, and the surf-spot admin service), constructed
  per request from the container's ``session_factory``.

The surf-spot admin service module (``brizocast.services.spot_admin_service``,
task 3.2) is created *later*; :func:`get_spot_admin_service` therefore imports it
**lazily inside the function** so this module imports cleanly today.

Requirements covered: 12.5, 13.4.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Request

from brizocast.admin.settings import PanelSettings
from brizocast.config.overrides import ConfigOverrideStore, OverrideAwareSettings
from brizocast.config.settings import load_settings
from brizocast.core.container import (
    FEEDBACK_SERVICE_KEY,
    FORECAST_CACHE_REPOSITORY_KEY,
    PRESET_SERVICE_KEY,
    SPOT_REPOSITORY_KEY,
    SUBSCRIPTION_SERVICE_KEY,
    USER_SERVICE_KEY,
    Container,
    SessionFactory,
)
from brizocast.database.session import create_engine, create_session_factory
from brizocast.repositories.forecast_cache_repo import SqlAlchemyForecastCacheRepository
from brizocast.repositories.json_spot_repo import JsonSpotRepository
from brizocast.services.admin_command_service import AdminCommandService
from brizocast.services.config_admin_service import ConfigAdminService
from brizocast.services.feedback_service import FeedbackService
from brizocast.services.preset_admin_service import PresetAdminService
from brizocast.services.preset_service import PresetService
from brizocast.services.sqlite_scheduler_state import SqliteSchedulerState
from brizocast.services.subscription_service import SubscriptionService
from brizocast.services.user_service import UserService

__all__ = [
    "build_overrides",
    "build_panel_container",
    "get_admin_command_service",
    "get_config_admin_service",
    "get_container",
    "get_feedback_service",
    "get_forecast_cache_repository",
    "get_overrides",
    "get_panel",
    "get_preset_admin_service",
    "get_preset_service",
    "get_scheduler_state",
    "get_session_factory",
    "get_spot_admin_service",
    "get_spot_repository",
    "get_subscription_service",
    "get_user_service",
]


# --------------------------------------------------------------------------- #
# Container construction (built once per shared database URL, then cached).
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=None)
def _build_container(database_url: str) -> Container:
    """Build the panel's own container at ``database_url`` (cached per URL).

    Reuses :func:`brizocast.config.settings.load_settings` for the ``.env``
    defaults that back the override accessor, and builds a fresh async engine +
    session factory over the shared database (Req 13.4). Cached so repeated
    builds for the same URL reuse one engine / connection pool.
    """
    settings = load_settings()
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    return Container(settings, session_factory=session_factory)


def build_panel_container(panel: PanelSettings) -> Container:
    """Return the panel's container pointed at the shared database (Req 12.5, 13.4)."""
    return _build_container(panel.DATABASE_URL)


def build_overrides(panel: PanelSettings) -> OverrideAwareSettings:
    """Build the override-aware settings facade over the shared override store.

    Wraps the bot's validated ``.env`` :class:`~brizocast.config.settings.Settings`
    (the override defaults) with a :class:`ConfigOverrideStore` on the shared
    session factory, so reads resolve override-first else ``.env`` (Req 12.2).
    """
    container = build_panel_container(panel)
    store = ConfigOverrideStore(container.session_factory)
    return OverrideAwareSettings(container.settings, store)


# --------------------------------------------------------------------------- #
# State accessors — read what the app factory set on ``request.app.state``.
# --------------------------------------------------------------------------- #


def get_container(request: Request) -> Container:
    """Return the panel container the app factory placed on ``app.state``."""
    container: Container = request.app.state.container
    return container


def get_panel(request: Request) -> PanelSettings:
    """Return the validated :class:`PanelSettings` from ``app.state``."""
    panel: PanelSettings = request.app.state.panel
    return panel


def get_overrides(request: Request) -> OverrideAwareSettings:
    """Return the :class:`OverrideAwareSettings` facade from ``app.state``."""
    overrides: OverrideAwareSettings = request.app.state.overrides
    return overrides


def get_session_factory(request: Request) -> SessionFactory:
    """Return the container's shared async session factory."""
    return get_container(request).session_factory


# --------------------------------------------------------------------------- #
# Reused services / repositories — resolved from the container by key.
# --------------------------------------------------------------------------- #


def get_user_service(request: Request) -> UserService:
    """Resolve the shared :class:`UserService` (Req 12.5)."""
    return get_container(request).resolve(USER_SERVICE_KEY, UserService)


def get_subscription_service(request: Request) -> SubscriptionService:
    """Resolve the shared :class:`SubscriptionService` (Req 12.5)."""
    return get_container(request).resolve(
        SUBSCRIPTION_SERVICE_KEY, SubscriptionService
    )


def get_preset_service(request: Request) -> PresetService:
    """Resolve the shared :class:`PresetService` (Req 12.5)."""
    return get_container(request).resolve(PRESET_SERVICE_KEY, PresetService)


def get_feedback_service(request: Request) -> FeedbackService:
    """Resolve the shared :class:`FeedbackService` (Req 12.5)."""
    return get_container(request).resolve(FEEDBACK_SERVICE_KEY, FeedbackService)


def get_forecast_cache_repository(
    request: Request,
) -> SqlAlchemyForecastCacheRepository:
    """Resolve the shared forecast-cache repository (Req 12.5)."""
    return get_container(request).resolve(
        FORECAST_CACHE_REPOSITORY_KEY, SqlAlchemyForecastCacheRepository
    )


def get_spot_repository(request: Request) -> JsonSpotRepository:
    """Resolve the shared :class:`JsonSpotRepository` (Req 12.5)."""
    return get_container(request).resolve(SPOT_REPOSITORY_KEY, JsonSpotRepository)


# --------------------------------------------------------------------------- #
# New admin services — built per request over the shared session factory.
# --------------------------------------------------------------------------- #


def get_config_admin_service(request: Request) -> ConfigAdminService:
    """Build the override writer/reader over the shared override store (Req 6.*, 7.*)."""
    store = ConfigOverrideStore(get_session_factory(request))
    return ConfigAdminService(store)


def get_admin_command_service(request: Request) -> AdminCommandService:
    """Build the admin command enqueuer over the shared session factory (Req 8.1, 9.1)."""
    return AdminCommandService(get_session_factory(request))


def get_preset_admin_service(request: Request) -> PresetAdminService:
    """Build the regional-preset admin service over the shared session factory (Req 5.*)."""
    return PresetAdminService(get_session_factory(request))


def get_scheduler_state(request: Request) -> SqliteSchedulerState:
    """Build the DB-backed scheduler-run reader over the shared session factory (Req 11.2)."""
    return SqliteSchedulerState(get_session_factory(request))


def get_spot_admin_service(request: Request) -> object:
    """Build the surf-spot admin service (Req 4.*).

    The concrete ``SpotAdminService`` module is created later (task 3.2), so it
    is imported **lazily here** to keep this module importable now. The provider
    returns it typed as :class:`object`; the spots router (task 5.5) narrows it
    to the concrete service once that module exists.
    """
    from brizocast.services.spot_admin_service import SpotAdminService

    panel = get_panel(request)
    service: object = SpotAdminService(panel.SPOT_DATASET_PATH)
    return service
