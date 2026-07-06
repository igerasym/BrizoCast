"""Admin-side writer/reader for live configuration overrides (Req 6.*, 7.*, 12.1).

The admin panel persists the three overridable settings — the monetization flag,
the per-tier plan-limit map, and the forecast-provider selection — into the
shared ``config_overrides`` table so the running bot picks them up live through
:class:`~brizocast.config.overrides.OverrideAwareSettings` (Req 6.4, 7.3, 12.2).
This service is the panel's single write boundary for those overrides; it wraps
:class:`~brizocast.config.overrides.ConfigOverrideStore` and validates each
submission **before** writing, leaving the store untouched on rejection.

Validation (each rejects without writing, raising :class:`ConfigValidationError`
so routers can map it to HTTP 400):

* Plan limits — every tier's ``max_subscriptions`` must be at least ``1``
  (Req 6.5).
* Forecast provider — the selected id must be a registered forecast provider
  key (Req 7.5).

Values are serialised to JSON-native shapes for the JSON column: the monetization
flag as a bool, plan limits as ``{tier: {max_subscriptions, notification_modes}}``
with ``notification_modes`` rendered as a **sorted list** (sets are not
JSON-serialisable), and the forecast provider as a plain string. On read, plan
limits are reconstructed into :class:`PlanLimit` (whose ``notification_modes``
``set`` field accepts the persisted list).

Requirements covered: 6.2, 6.3, 6.5, 7.2, 7.5, 12.1.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from brizocast.config.overrides import ConfigOverrideStore, VALID_LOG_LEVELS
from brizocast.config.settings import PlanLimit
from brizocast.providers.forecast.factory import (
    _REGISTRY,
    forecast_provider_label,
    registered_forecast_provider_keys,
)

__all__ = [
    "ConfigAdminService",
    "ConfigValidationError",
    "ForecastProviderInfo",
]

# Override keys (mirror :data:`brizocast.config.overrides.OVERRIDE_KEYS`). Kept
# as named constants so the write and read paths cannot drift apart.
_MONETIZATION_ENABLED_KEY = "MONETIZATION_ENABLED"
_PLAN_LIMITS_KEY = "PLAN_LIMITS"
_FORECAST_PROVIDER_KEY = "FORECAST_PROVIDER"
_LOG_LEVEL_KEY = "LOG_LEVEL"
# Persisted list of forecast provider keys the admin has enabled. Absent ⇒ every
# registered provider is enabled (the default).
_FORECAST_PROVIDERS_ENABLED_KEY = "FORECAST_PROVIDERS_ENABLED"


class ConfigValidationError(ValueError):
    """Raised when an override submission is invalid and must not be persisted.

    Subclasses :class:`ValueError` so routers can catch it and map it to an HTTP
    400 response while leaving the override store unchanged (Req 6.5, 7.5).
    """


@dataclass(frozen=True, slots=True)
class ForecastProviderInfo:
    """Presentation-ready row for the admin "forecast providers" section.

    Attributes:
        key: The provider's stable registry key.
        label: A human-readable provider name for the UI.
        enabled: Whether the provider is currently enabled (selectable).
        active: Whether the provider is the currently-selected active one.
    """

    key: str
    label: str
    enabled: bool
    active: bool


def _coerce_bool(value: object) -> bool:
    """Coerce a JSON-decoded override into a ``bool`` defensively."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class ConfigAdminService:
    """Write and read the three runtime overrides via :class:`ConfigOverrideStore`.

    The service owns no session itself; every call delegates to the injected
    store, which opens one unit of work per access. Writes validate first and
    only persist when validation passes, so a rejected submission never mutates
    the store (Req 6.5, 7.5, 12.1).
    """

    def __init__(self, store: ConfigOverrideStore) -> None:
        """Initialise the service.

        Args:
            store: The shared ``config_overrides`` repository the panel and bot
                rendezvous on.
        """
        self._store = store

    # -- monetization flag --------------------------------------------------- #

    async def set_monetization_enabled(self, enabled: bool) -> None:
        """Persist the monetization flag as a JSON bool override (Req 6.2)."""
        await self._store.set(_MONETIZATION_ENABLED_KEY, bool(enabled))

    async def read_monetization_enabled(self) -> bool | None:
        """Return the persisted monetization flag, or ``None`` if unset."""
        raw = await self._store.get(_MONETIZATION_ENABLED_KEY)
        return None if raw is None else _coerce_bool(raw)

    # -- plan limits --------------------------------------------------------- #

    async def set_plan_limits(self, plan_limits: Mapping[str, PlanLimit]) -> None:
        """Persist the per-tier plan-limit map as a JSON override (Req 6.3).

        Validates every tier's ``max_subscriptions`` is at least ``1`` and
        rejects the whole submission — writing nothing — if any tier fails
        (Req 6.5). ``notification_modes`` is serialised as a sorted list so the
        value is JSON-native and stable.

        Args:
            plan_limits: Mapping of plan-tier key to its :class:`PlanLimit`.

        Raises:
            ConfigValidationError: If any tier's ``max_subscriptions`` is below
                ``1``. The store is left unchanged.
        """
        serialised: dict[str, dict[str, object]] = {}
        for tier, limit in plan_limits.items():
            if limit.max_subscriptions < 1:
                raise ConfigValidationError(
                    f"plan tier {tier!r} max_subscriptions must be >= 1, "
                    f"got {limit.max_subscriptions}"
                )
            serialised[str(tier)] = {
                "max_subscriptions": limit.max_subscriptions,
                "notification_modes": sorted(limit.notification_modes),
            }
        await self._store.set(_PLAN_LIMITS_KEY, serialised)

    async def read_plan_limits(self) -> dict[str, PlanLimit] | None:
        """Return the persisted plan-limit map, or ``None`` if unset.

        Reconstructs each entry into a :class:`PlanLimit`; the persisted
        ``notification_modes`` list rehydrates the model's ``set`` field.
        """
        raw = await self._store.get(_PLAN_LIMITS_KEY)
        if not isinstance(raw, dict):
            return None
        return {str(tier): PlanLimit(**value) for tier, value in raw.items()}

    # -- forecast provider --------------------------------------------------- #

    async def set_forecast_provider(self, provider: str) -> None:
        """Persist the forecast-provider selection as a JSON string (Req 7.2).

        Validates the id against the forecast registry keys and that it is
        currently **enabled**, rejecting otherwise — writing nothing (Req 7.5).

        Args:
            provider: The forecast provider id to select.

        Raises:
            ConfigValidationError: If ``provider`` is not a registered provider
                key, or is registered but currently disabled. The store is left
                unchanged.
        """
        if provider not in _REGISTRY:
            available = ", ".join(sorted(_REGISTRY))
            raise ConfigValidationError(
                f"unknown forecast provider {provider!r}; available: {available}"
            )
        if provider not in await self.enabled_forecast_providers():
            raise ConfigValidationError(
                f"forecast provider {provider!r} is disabled; enable it first"
            )
        await self._store.set(_FORECAST_PROVIDER_KEY, provider)

    async def read_forecast_provider(self) -> str | None:
        """Return the persisted forecast-provider id, or ``None`` if unset."""
        raw = await self._store.get(_FORECAST_PROVIDER_KEY)
        return None if raw is None else str(raw)

    def available_forecast_providers(self) -> list[str]:
        """Return the sorted list of registered forecast provider ids (Req 7.1)."""
        return registered_forecast_provider_keys()

    # -- log level ----------------------------------------------------------- #

    async def set_log_level(self, level: str) -> None:
        """Persist the log level as a JSON string override.

        Validates the level against the recognised logging levels, rejecting
        otherwise — writing nothing.

        Args:
            level: The log level name (e.g. ``"DEBUG"``, ``"INFO"``).

        Raises:
            ConfigValidationError: If ``level`` is not a recognised level.
        """
        normalised = level.strip().upper()
        if normalised not in VALID_LOG_LEVELS:
            available = ", ".join(sorted(VALID_LOG_LEVELS))
            raise ConfigValidationError(
                f"unknown log level {level!r}; available: {available}"
            )
        await self._store.set(_LOG_LEVEL_KEY, normalised)

    async def read_log_level(self) -> str | None:
        """Return the persisted log level, or ``None`` if unset."""
        raw = await self._store.get(_LOG_LEVEL_KEY)
        return None if raw is None else str(raw)

    # -- enable / disable providers ----------------------------------------- #

    async def enabled_forecast_providers(self) -> list[str]:
        """Return the enabled provider keys, sorted (Req 7.1).

        Resolves the persisted ``FORECAST_PROVIDERS_ENABLED`` override, dropping
        any keys no longer registered. When no override is persisted (the
        default) **every** registered provider is considered enabled.
        """
        raw = await self._store.get(_FORECAST_PROVIDERS_ENABLED_KEY)
        if not isinstance(raw, list):
            return registered_forecast_provider_keys()
        enabled = {str(key) for key in raw if str(key) in _REGISTRY}
        if not enabled:
            # A persisted-but-empty/stale set would leave no provider; treat as
            # "all enabled" so the bot always has a working source.
            return registered_forecast_provider_keys()
        return sorted(enabled)

    async def set_forecast_providers_enabled(
        self, keys: Iterable[str], *, active_provider: str
    ) -> None:
        """Persist the set of enabled forecast providers (Req 7.1).

        Validates that every key is a registered provider, that at least one
        provider remains enabled, and that the currently-active ``active_provider``
        stays enabled (so the bot never points at a disabled source). Rejects the
        whole submission — writing nothing — on any violation.

        Args:
            keys: The provider keys to enable.
            active_provider: The currently-resolved active provider id, which
                must remain in the enabled set.

        Raises:
            ConfigValidationError: On an unknown key, an empty set, or an attempt
                to disable the active provider. The store is left unchanged.
        """
        requested = {str(key) for key in keys}
        unknown = sorted(requested - set(_REGISTRY))
        if unknown:
            raise ConfigValidationError(
                f"unknown forecast provider(s): {', '.join(unknown)}"
            )
        if not requested:
            raise ConfigValidationError(
                "at least one forecast provider must remain enabled"
            )
        if active_provider not in requested:
            raise ConfigValidationError(
                f"cannot disable the active provider "
                f"{forecast_provider_label(active_provider)!r}; "
                "switch the active provider first"
            )
        await self._store.set(
            _FORECAST_PROVIDERS_ENABLED_KEY, sorted(requested)
        )

    async def forecast_provider_overview(
        self, active_provider: str
    ) -> list[ForecastProviderInfo]:
        """Return one :class:`ForecastProviderInfo` per registered provider.

        Combines the registry, the enabled set, and the resolved
        ``active_provider`` into rows the admin "providers" section renders with
        an enable/disable toggle and an "active" marker (Req 7.1).
        """
        enabled = set(await self.enabled_forecast_providers())
        return [
            ForecastProviderInfo(
                key=key,
                label=forecast_provider_label(key),
                enabled=key in enabled,
                active=key == active_provider,
            )
            for key in registered_forecast_provider_keys()
        ]
