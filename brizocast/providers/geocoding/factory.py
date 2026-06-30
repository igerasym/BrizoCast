"""Factory that resolves the configured geocoding provider.

:func:`build_geocoding_provider` maps ``Settings.GEOCODING_PROVIDER`` to a
concrete :class:`~brizocast.core.ports.geocoding_provider.GeocodingProvider`
implementation. An unknown or empty key falls back to the Open-Meteo Geocoding
default (mirroring the forecast-provider default behaviour of Req 15.5), so a
misconfigured value degrades to a working provider rather than failing startup.

This module only constructs the provider; wiring it into the DI container
happens elsewhere at composition time.
"""

from __future__ import annotations

import httpx

from brizocast.config.settings import Settings
from brizocast.core.logging import get_logger
from brizocast.core.ports.geocoding_provider import GeocodingProvider
from brizocast.providers.geocoding.open_meteo_geocoding import (
    OPEN_METEO_GEOCODING_KEY,
    OpenMeteoGeocodingProvider,
)

__all__ = ["build_geocoding_provider"]

_log = get_logger(__name__)


def build_geocoding_provider(
    cfg: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> GeocodingProvider:
    """Build the geocoding provider selected by configuration.

    Args:
        cfg: Validated application settings; ``cfg.GEOCODING_PROVIDER`` selects
            the implementation.
        client: Optional shared :class:`httpx.AsyncClient` injected into the
            provider so the caller owns the client lifecycle and timeout.

    Returns:
        A :class:`GeocodingProvider`. An unknown or empty
        ``GEOCODING_PROVIDER`` resolves to the Open-Meteo Geocoding default.
    """
    key = (cfg.GEOCODING_PROVIDER or "").strip().lower()

    if key and key != OPEN_METEO_GEOCODING_KEY:
        _log.warning(
            "Unknown GEOCODING_PROVIDER %r; falling back to %s",
            cfg.GEOCODING_PROVIDER,
            OPEN_METEO_GEOCODING_KEY,
        )

    # Only the Open-Meteo Geocoding implementation exists in the MVP; every
    # other (including empty/unknown) key resolves to it as the default.
    return OpenMeteoGeocodingProvider(client=client)
