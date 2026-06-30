"""Dependency-injection container — the composition root's backbone.

The :class:`Container` is the single place that holds BrizoCast's cross-cutting
singletons and, over time, wires concrete adapters to the ports defined in
``core/ports``. Following the Clean-Architecture dependency rule, only the
composition root (``core/container.py`` together with ``bot/app.py``) knows about
every layer; the rest of the code depends on this container and on ports, never
on concrete implementations.

This module is the **skeleton** delivered in milestone *M1 — Foundations*. It
holds the three cross-cutting concerns that already exist:

* the validated :class:`~brizocast.config.settings.Settings`,
* an injected async session factory hook (an ``async_sessionmaker``), and
* a logger factory (defaulting to
  :func:`~brizocast.core.logging.get_logger`).

Everything else — forecast / geocoding / AI provider factories, repositories,
domain services, the notification engine, and the scheduler — is filled in by
later milestones through the lazy registration hooks
(:meth:`Container.register` / :meth:`Container.resolve`). To stay importable
*now*, this module deliberately imports **no** provider, repository, service,
notification, or scheduler module (several of which do not exist yet) and takes
no hard dependency on database/session internals beyond the injected factory
callable.

Requirements covered: 18.6 (and the architectural foundation for the composition
root used by 11.1).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.config.settings import Settings
from brizocast.core.errors import ConfigurationError, NotFoundError
from brizocast.core.logging import BoundLogger, get_logger

__all__ = [
    "AI_PROVIDER_KEY",
    "Container",
    "ENTITLEMENT_SERVICE_KEY",
    "FEEDBACK_SERVICE_KEY",
    "FORECAST_CACHE_REPOSITORY_KEY",
    "FORECAST_PROVIDER_KEY",
    "FORECAST_SERVICE_KEY",
    "GEOCODING_PROVIDER_KEY",
    "LOCATION_SERVICE_KEY",
    "NOTIFICATION_ENGINE_KEY",
    "NOTIFICATION_SERVICE_KEY",
    "PLAN_EXPIRY_SERVICE_KEY",
    "PRESET_SERVICE_KEY",
    "SPOT_DISCOVERY_SERVICE_KEY",
    "SPOT_REPOSITORY_KEY",
    "SUBSCRIPTION_SERVICE_KEY",
    "USER_SERVICE_KEY",
    "LoggerFactory",
    "SessionFactory",
]

T = TypeVar("T")

# Stable registration keys for the external-data provider singletons wired by
# milestone M3 (tasks 3.2, 3.5, 3.10). Components resolve a provider by passing
# the matching port type to :meth:`Container.resolve`.
FORECAST_PROVIDER_KEY = "forecast_provider"
GEOCODING_PROVIDER_KEY = "geocoding_provider"
AI_PROVIDER_KEY = "ai_provider"

# M5 — the notification engine singleton (task 5.1). Resolved by the scheduler's
# forecast-check job (task 8.1) to decide which alerts to dispatch / buffer.
NOTIFICATION_ENGINE_KEY = "notification_engine"
# M9 — monetization. The entitlement gate singleton (task 10.1) and the
# subscription service wired to consult it at its single creation touch-point.
ENTITLEMENT_SERVICE_KEY = "entitlement_service"
SUBSCRIPTION_SERVICE_KEY = "subscription_service"

# M10 — composition root (task 11.1). The remaining application services and
# infrastructure repositories wired as lazy singletons so the bootstrap
# (``bot/app.py``) resolves a single shared instance of each. Telegram- and
# scheduler-bound collaborators (the message sender, the digest/forecast jobs,
# and the APScheduler runner) need the live ``Application`` and are assembled in
# ``bot/app.py`` rather than here.
USER_SERVICE_KEY = "user_service"
LOCATION_SERVICE_KEY = "location_service"
PRESET_SERVICE_KEY = "preset_service"
FEEDBACK_SERVICE_KEY = "feedback_service"
NOTIFICATION_SERVICE_KEY = "notification_service"
FORECAST_SERVICE_KEY = "forecast_service"
SPOT_DISCOVERY_SERVICE_KEY = "spot_discovery_service"
PLAN_EXPIRY_SERVICE_KEY = "plan_expiry_service"
SPOT_REPOSITORY_KEY = "spot_repository"
FORECAST_CACHE_REPOSITORY_KEY = "forecast_cache_repository"

# The async session factory hook. ``async_sessionmaker`` is itself a callable
# returning an :class:`AsyncSession`, so the container depends only on this
# injected factory and never on the engine/bootstrap internals (filled by M1
# task 1.5, ``database/session.py``).
SessionFactory = async_sessionmaker[AsyncSession]


class LoggerFactory(Protocol):
    """Callable that produces a structured :class:`BoundLogger`.

    Matches the signature of :func:`brizocast.core.logging.get_logger` so the
    real factory can be injected directly, while tests may substitute a fake.
    """

    def __call__(self, name: str, **context: object) -> BoundLogger:
        """Return a logger for ``name`` bound to the given structured context."""
        ...


class Container:
    """Holds cross-cutting singletons and wires adapters to ports at startup.

    The container is constructed from a validated :class:`Settings` instance and
    optionally an async session factory and logger factory. Components added by
    later milestones are registered as lazily-instantiated singletons via
    :meth:`register` and retrieved, type-checked, via :meth:`resolve`.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: SessionFactory | None = None,
        logger_factory: LoggerFactory = get_logger,
    ) -> None:
        """Initialise the container.

        Args:
            settings: The validated application configuration.
            session_factory: Optional async session factory. May be ``None`` at
                construction time (e.g. before the database is bootstrapped) and
                injected later; accessing :attr:`session_factory` before one is
                provided raises :class:`ConfigurationError`.
            logger_factory: Factory used by :meth:`get_logger`. Defaults to the
                package's structured :func:`get_logger`.
        """
        self._settings = settings
        self._session_factory = session_factory
        self._logger_factory = logger_factory

        # Lazy registration hooks. ``_factories`` maps a registration key to a
        # builder invoked at most once; the result is cached in ``_singletons``.
        # This is how later milestones wire concrete adapters to ports without
        # this module importing them:
        #
        #   TODO(M3/M4 — persistence): register the spot repository and the
        #       SQLAlchemy repositories (tasks 3.6, 3.8, 4.1).
        #   TODO(M4 — services): register UserService, LocationService,
        #       SubscriptionService, PresetService, etc. (tasks 4.3-4.13).
        #   TODO(M5 — notifications): register the NotificationEngine and the
        #       Telegram sender (tasks 5.1, 5.7).
        #   TODO(M7 — scheduler): register the scheduler runner / jobs
        #       (tasks 8.1, 8.3, 8.5).
        self._factories: dict[str, Callable[[Container], object]] = {}
        self._singletons: dict[str, object] = {}

        # M3 — wire the external-data provider factories (forecast, geocoding,
        # AI). Registered lazily: the concrete factory modules are imported only
        # when the provider is first resolved, so the container stays importable
        # even while sibling tasks (3.2 forecast, 3.5 geocoding) are still in
        # flight, and the Clean-Architecture import direction is preserved.
        self._register_provider_factories()

        # M5 — wire the notification engine lazily. Its builder constructs a
        # NotificationService (over the injected session factory) and an
        # AntiSpamConfig from settings the first time it is resolved, by which
        # point the composition root has supplied the session factory.
        self._register_notification_engine()

        # M9 — wire the monetization gate lazily. The EntitlementService and the
        # gated SubscriptionService are built on first resolve, once the session
        # factory is present; the SubscriptionService is wired to consult the
        # entitlement gate at its single ``on_before_create`` touch-point.
        self._register_monetization()

        # M10 — wire the remaining application services and the infrastructure
        # repositories they depend on (task 11.1). Lazy singletons built on first
        # resolve, by which point the composition root has supplied the session
        # factory; the concrete modules are imported inside the builders to keep
        # the container importable and the Clean-Architecture import direction.
        self._register_services()

    # -- notification engine wiring (M5) ------------------------------------- #

    def _register_notification_engine(self) -> None:
        """Register the :class:`NotificationEngine` as a lazy singleton.

        The concrete notification modules are imported inside the builder (not
        at module load) so the container stays importable independently of the
        notification stack and the Clean-Architecture import direction is kept:
        only the composition root knows the concrete wiring.
        """

        def _build_notification_engine(container: Container) -> object:
            from brizocast.core.domain.antispam import AntiSpamConfig
            from brizocast.notifications.engine import NotificationEngine
            from brizocast.services.notification_service import NotificationService

            history = NotificationService(
                container.session_factory,
                logger=container.get_logger("notification_service"),
            )
            cfg = AntiSpamConfig(
                significant_improvement=container.settings.SIGNIFICANT_IMPROVEMENT
            )
            return NotificationEngine(
                history,
                cfg,
                logger=container.get_logger("notification_engine"),
            )

        self.register(NOTIFICATION_ENGINE_KEY, _build_notification_engine)

    # -- monetization wiring (M9) -------------------------------------------- #

    def _register_monetization(self) -> None:
        """Register the entitlement gate and the SubscriptionService it gates.

        Both are lazy singletons; the concrete service modules are imported
        inside the builders so the container stays importable independently of
        the service layer and the Clean-Architecture import direction is kept.

        The wiring is the heart of the monetization design: the
        ``SubscriptionService`` is constructed with
        ``on_before_create=entitlement.assert_can_create_subscription`` — its
        single, clearly-marked gate call site. While ``MONETIZATION_ENABLED`` is
        false the gate is a no-op, so MVP creation is unchanged (Req 21.2, 21.6);
        when enabled it raises ``QuotaExceededError`` at the tier limit
        (Req 21.4). Limits and the flag come entirely from settings (Req 21.7).
        """

        def _build_entitlement_service(container: Container) -> object:
            from brizocast.config.overrides import (
                ConfigOverrideStore,
                OverrideAwareSettings,
            )
            from brizocast.services.entitlement_service import EntitlementService

            return EntitlementService(
                container.session_factory,
                OverrideAwareSettings(
                    container.settings,
                    ConfigOverrideStore(container.session_factory),
                ),
                logger=container.get_logger("entitlement_service"),
            )

        def _build_subscription_service(container: Container) -> object:
            from brizocast.services.entitlement_service import EntitlementService
            from brizocast.services.subscription_service import SubscriptionService

            entitlement = container.resolve(
                ENTITLEMENT_SERVICE_KEY, EntitlementService
            )
            return SubscriptionService(
                container.session_factory,
                on_before_create=entitlement.assert_can_create_subscription,
                logger=container.get_logger("subscription_service"),
            )

        self.register(ENTITLEMENT_SERVICE_KEY, _build_entitlement_service)
        self.register(SUBSCRIPTION_SERVICE_KEY, _build_subscription_service)

    # -- application service / repository wiring (M10) ----------------------- #

    def _register_services(self) -> None:
        """Register the remaining services and infrastructure repositories.

        Every builder is a lazy singleton; the concrete service / repository
        modules are imported inside the closures so the container stays
        importable independently of the service and infrastructure layers and
        the composition root keeps the only knowledge of concrete wiring. Each
        provider/repository dependency is resolved back through this container so
        a single shared instance is reused across the services that need it.
        """

        def _build_spot_repository(container: Container) -> object:
            from brizocast.repositories.json_spot_repo import JsonSpotRepository

            return JsonSpotRepository(
                dataset_path=container.settings.SPOT_DATASET_PATH,
                logger=container.get_logger("json_spot_repository"),
            )

        def _build_forecast_cache_repository(container: Container) -> object:
            from brizocast.repositories.forecast_cache_repo import (
                SqlAlchemyForecastCacheRepository,
            )

            return SqlAlchemyForecastCacheRepository(
                container.session_factory,
                logger=container.get_logger("forecast_cache_repository"),
            )

        def _build_user_service(container: Container) -> object:
            from brizocast.services.user_service import UserService

            return UserService(
                container.session_factory,
                logger=container.get_logger("user_service"),
            )

        def _build_location_service(container: Container) -> object:
            from brizocast.providers.geocoding.factory import (
                build_geocoding_provider,
            )
            from brizocast.providers.geocoding.reverse import NominatimReverseGeocoder
            from brizocast.services.location_service import LocationService

            return LocationService(
                container.session_factory,
                geocoding_provider=build_geocoding_provider(container.settings),
                reverse_geocoder=NominatimReverseGeocoder(
                    logger=container.get_logger("reverse_geocoder"),
                ),
                logger=container.get_logger("location_service"),
            )

        def _build_preset_service(container: Container) -> object:
            from brizocast.providers.ai.factory import build_ai_provider
            from brizocast.services.preset_service import PresetService

            return PresetService(
                container.session_factory,
                ai_provider=build_ai_provider(container.settings),
                logger=container.get_logger("preset_service"),
            )

        def _build_feedback_service(container: Container) -> object:
            from brizocast.services.feedback_service import FeedbackService

            return FeedbackService(
                container.session_factory,
                logger=container.get_logger("feedback_service"),
            )

        def _build_notification_service(container: Container) -> object:
            from brizocast.services.notification_service import NotificationService

            return NotificationService(
                container.session_factory,
                logger=container.get_logger("notification_service"),
            )

        def _build_forecast_service(container: Container) -> object:
            from datetime import timedelta

            from brizocast.providers.forecast.factory import (
                build_forecast_provider,
            )
            from brizocast.repositories.forecast_cache_repo import (
                SqlAlchemyForecastCacheRepository,
            )
            from brizocast.services.forecast_service import ForecastService

            return ForecastService(
                build_forecast_provider(container.settings),
                container.resolve(
                    FORECAST_CACHE_REPOSITORY_KEY, SqlAlchemyForecastCacheRepository
                ),
                timedelta(minutes=container.settings.FORECAST_CACHE_TTL_MINUTES),
                logger=container.get_logger("forecast_service"),
            )

        def _build_spot_discovery_service(container: Container) -> object:
            from brizocast.repositories.json_spot_repo import JsonSpotRepository
            from brizocast.services.spot_discovery_service import SpotDiscoveryService

            return SpotDiscoveryService(
                container.resolve(SPOT_REPOSITORY_KEY, JsonSpotRepository),
                logger=container.get_logger("spot_discovery_service"),
            )

        def _build_plan_expiry_service(container: Container) -> object:
            from brizocast.services.plan_expiry_service import PlanExpiryService

            return PlanExpiryService(
                container.session_factory,
                logger=container.get_logger("plan_expiry_service"),
            )

        self.register(SPOT_REPOSITORY_KEY, _build_spot_repository)
        self.register(FORECAST_CACHE_REPOSITORY_KEY, _build_forecast_cache_repository)
        self.register(USER_SERVICE_KEY, _build_user_service)
        self.register(LOCATION_SERVICE_KEY, _build_location_service)
        self.register(PRESET_SERVICE_KEY, _build_preset_service)
        self.register(FEEDBACK_SERVICE_KEY, _build_feedback_service)
        self.register(NOTIFICATION_SERVICE_KEY, _build_notification_service)
        self.register(FORECAST_SERVICE_KEY, _build_forecast_service)
        self.register(SPOT_DISCOVERY_SERVICE_KEY, _build_spot_discovery_service)
        self.register(PLAN_EXPIRY_SERVICE_KEY, _build_plan_expiry_service)

    # -- provider factory wiring (M3) ---------------------------------------- #

    def _register_provider_factories(self) -> None:
        """Register the forecast, geocoding, and AI provider factories.

        Each provider is registered as a lazily-instantiated singleton built
        from :attr:`settings`. The concrete factory modules are imported inside
        the builder closures (not at module load) so this container remains
        importable while the forecast/geocoding factories (tasks 3.2 / 3.5) are
        still being implemented in parallel, and so the composition root keeps
        the only knowledge of concrete adapters.
        """

        def _build_forecast_provider(container: Container) -> object:
            from brizocast.providers.forecast.factory import build_forecast_provider

            return build_forecast_provider(container.settings)

        def _build_geocoding_provider(container: Container) -> object:
            from brizocast.providers.geocoding.factory import build_geocoding_provider

            return build_geocoding_provider(container.settings)

        def _build_ai_provider(container: Container) -> object:
            from brizocast.providers.ai.factory import build_ai_provider

            return build_ai_provider(container.settings)

        self.register(FORECAST_PROVIDER_KEY, _build_forecast_provider)
        self.register(GEOCODING_PROVIDER_KEY, _build_geocoding_provider)
        self.register(AI_PROVIDER_KEY, _build_ai_provider)

    # -- cross-cutting singletons available now ------------------------------ #

    @property
    def settings(self) -> Settings:
        """The validated application configuration."""
        return self._settings

    @property
    def session_factory(self) -> SessionFactory:
        """The injected async session factory.

        Raises:
            ConfigurationError: If no session factory has been provided yet.
        """
        if self._session_factory is None:
            raise ConfigurationError(
                "No async session factory has been registered on the container. "
                "Inject one via Container(session_factory=...) or "
                "set_session_factory() before resolving database-backed "
                "components.",
                field="session_factory",
            )
        return self._session_factory

    @property
    def has_session_factory(self) -> bool:
        """Whether an async session factory has been provided."""
        return self._session_factory is not None

    def set_session_factory(self, session_factory: SessionFactory) -> None:
        """Inject the async session factory after construction.

        Used by the composition root once the database has been bootstrapped
        (M1 task 1.5) so the container can be built before the engine exists.
        """
        self._session_factory = session_factory

    def get_logger(self, name: str, **context: object) -> BoundLogger:
        """Return a structured logger bound to the given context.

        Delegates to the injected :class:`LoggerFactory`, so all components
        obtain loggers through the container.
        """
        return self._logger_factory(name, **context)

    # -- lazy registration hooks for later milestones ------------------------ #

    def register(self, key: str, factory: Callable[[Container], object]) -> None:
        """Register a lazily-instantiated singleton under ``key``.

        The ``factory`` receives this container (so it can resolve its own
        dependencies) and is invoked at most once, the first time ``key`` is
        resolved. Registering a key that already has a cached instance discards
        that instance so the new factory takes effect on the next resolve.

        Args:
            key: Stable identifier for the component (e.g.
                ``"forecast_provider_factory"``).
            factory: Callable building the component from the container.
        """
        self._factories[key] = factory
        self._singletons.pop(key, None)

    def register_instance(self, key: str, instance: object) -> None:
        """Register an already-constructed singleton under ``key``.

        Convenience for components that are cheap to build eagerly or supplied
        by tests; the instance is returned as-is by :meth:`resolve`.
        """
        self._singletons[key] = instance
        self._factories.pop(key, None)

    def is_registered(self, key: str) -> bool:
        """Whether a factory or instance is registered under ``key``."""
        return key in self._factories or key in self._singletons

    def resolve(self, key: str, expected_type: type[T]) -> T:
        """Resolve the singleton registered under ``key``, type-checked.

        Instantiates the registered factory on first use and caches the result.

        Args:
            key: The registration key.
            expected_type: The type the resolved component must be an instance
                of; guards against mis-registration at the composition root.

        Returns:
            The cached singleton, statically typed as ``expected_type``.

        Raises:
            NotFoundError: If nothing is registered under ``key``.
            ConfigurationError: If the resolved component is not an instance of
                ``expected_type``.
        """
        if key not in self._singletons:
            factory = self._factories.get(key)
            if factory is None:
                raise NotFoundError(
                    f"No component registered under key {key!r}."
                )
            self._singletons[key] = factory(self)

        instance = self._singletons[key]
        if not isinstance(instance, expected_type):
            raise ConfigurationError(
                f"Component registered under {key!r} is "
                f"{type(instance).__name__}, expected "
                f"{expected_type.__name__}.",
                field=key,
            )
        return instance
