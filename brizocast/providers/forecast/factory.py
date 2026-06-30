"""Forecast provider factory.

:func:`build_forecast_provider` resolves the configured ``FORECAST_PROVIDER``
key to a concrete :class:`~brizocast.core.ports.forecast_provider.ForecastProvider`
implementation (Req 6.3). An unknown or empty key falls back to the Open-Meteo
Marine default (Req 15.5), so the system always has a working provider.

New providers are added by registering a builder in :data:`_REGISTRY`; the
scoring and notification engines never reference these implementations, so
adding one requires no changes elsewhere (Req 6.6).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from brizocast.config.settings import Settings
from brizocast.core.logging import get_logger
from brizocast.core.ports.forecast_provider import ForecastProvider
from brizocast.providers.forecast.open_meteo_marine import OpenMeteoMarineProvider
from brizocast.providers.forecast.stormglass import StormglassProvider
from brizocast.providers.forecast.windy import WindyProvider

__all__ = [
    "DEFAULT_FORECAST_PROVIDER_KEY",
    "build_forecast_provider",
    "forecast_provider_label",
    "registered_forecast_provider_keys",
]

logger = get_logger(__name__)

# The default key used when the configured value is empty or unrecognised.
DEFAULT_FORECAST_PROVIDER_KEY = OpenMeteoMarineProvider.key

# A builder takes an optional shared httpx client and returns a provider bound
# to this port. Keyed by the provider's stable ``key``.
_ProviderBuilder = Callable[[httpx.AsyncClient | None], ForecastProvider]

_REGISTRY: dict[str, _ProviderBuilder] = {
    OpenMeteoMarineProvider.key: OpenMeteoMarineProvider,
    StormglassProvider.key: StormglassProvider,
    WindyProvider.key: WindyProvider,
}

# Human-readable labels for the admin UI. A provider missing here falls back to
# a title-cased form of its key, so newly-registered providers still render.
_PROVIDER_LABELS: dict[str, str] = {
    OpenMeteoMarineProvider.key: "Open-Meteo Marine",
    StormglassProvider.key: "Stormglass",
    WindyProvider.key: "Windy",
}


def registered_forecast_provider_keys() -> list[str]:
    """Return every registered forecast provider key, sorted (admin listing)."""
    return sorted(_REGISTRY)


def forecast_provider_label(key: str) -> str:
    """Return a human-readable label for ``key`` (title-cased key as fallback)."""
    return _PROVIDER_LABELS.get(key, key.replace("_", " ").title())


def build_forecast_provider(
    cfg: Settings, *, client: httpx.AsyncClient | None = None
) -> ForecastProvider:
    """Resolve ``cfg.FORECAST_PROVIDER`` to a :class:`ForecastProvider`.

    Args:
        cfg: Validated application settings carrying ``FORECAST_PROVIDER``.
        client: Optional shared :class:`httpx.AsyncClient` injected into the
            built provider; when ``None`` the provider manages its own client.

    Returns:
        The provider matching the configured key, or the Open-Meteo Marine
        default when the key is empty or unrecognised (Req 15.5).
    """

    requested = (cfg.FORECAST_PROVIDER or "").strip().lower()
    builder = _REGISTRY.get(requested)
    if builder is None:
        if requested:
            logger.warning(
                "Unknown forecast provider %r; falling back to %s",
                requested,
                DEFAULT_FORECAST_PROVIDER_KEY,
            )
        builder = _REGISTRY[DEFAULT_FORECAST_PROVIDER_KEY]
    return builder(client)
