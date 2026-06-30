"""Live forecast-provider selection (Req 7.3).

The forecast provider is one of the runtime-overridable settings: an admin can
switch it from the panel, which persists a ``FORECAST_PROVIDER`` override in the
shared database. :class:`ProviderSelector` resolves that override
**per scheduler tick** so the change takes effect on the next forecast-check run
without restarting the bot.

It reads the resolved provider id through
:class:`~brizocast.config.overrides.OverrideAwareSettings` (override-first, else
the ``.env`` default) and builds the matching
:class:`~brizocast.core.ports.forecast_provider.ForecastProvider` via
:func:`~brizocast.providers.forecast.factory.build_forecast_provider`, which
falls back safely to the Open-Meteo Marine default on an unknown key.

To avoid rebuilding a provider (and its HTTP client) on every tick, the selector
caches the built provider and only rebuilds it when the resolved key changes.

Requirements covered: 7.3.
"""

from __future__ import annotations

from collections.abc import Callable

from brizocast.config.overrides import OverrideAwareSettings
from brizocast.config.settings import Settings
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.core.ports.forecast_provider import ForecastProvider
from brizocast.providers.forecast.factory import build_forecast_provider

__all__ = ["ProviderSelector"]

# Builds a provider from a (provider-overridden) Settings. Injectable for tests.
ProviderBuilder = Callable[[Settings], ForecastProvider]


class ProviderSelector:
    """Resolves and caches the live forecast provider per tick (Req 7.3)."""

    def __init__(
        self,
        overrides: OverrideAwareSettings,
        base: Settings,
        *,
        builder: ProviderBuilder = build_forecast_provider,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the selector.

        Args:
            overrides: Override-aware settings facade used to resolve the live
                ``FORECAST_PROVIDER`` (override-first, else ``.env``).
            base: The validated ``.env`` settings; copied with the resolved
                provider id when building a provider.
            builder: Factory building a provider from settings; injectable for
                tests (defaults to :func:`build_forecast_provider`).
            logger: Optional bound logger; one is created when omitted.
        """
        self._overrides = overrides
        self._base = base
        self._builder = builder
        self._log = logger or get_logger(__name__)
        self._cached_key: str | None = None
        self._cached_provider: ForecastProvider | None = None

    async def current(self) -> ForecastProvider:
        """Return the provider for the currently-resolved provider id (Req 7.3).

        Re-reads the resolved id on every call and rebuilds the provider only
        when the id changed since the last build, so a provider switch made in
        the panel applies on the next tick without churning HTTP clients.
        """
        key = await self._overrides.forecast_provider()
        if self._cached_provider is None or key != self._cached_key:
            cfg = self._base.model_copy(update={"FORECAST_PROVIDER": key})
            self._cached_provider = self._builder(cfg)
            if self._cached_key is not None and key != self._cached_key:
                self._log.info(
                    "forecast provider switched: %s -> %s", self._cached_key, key
                )
            self._cached_key = key
        return self._cached_provider
