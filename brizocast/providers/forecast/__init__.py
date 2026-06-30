"""Forecast provider implementations (Open-Meteo Marine default) and factory."""

from __future__ import annotations

from brizocast.providers.forecast.factory import (
    DEFAULT_FORECAST_PROVIDER_KEY,
    build_forecast_provider,
)
from brizocast.providers.forecast.open_meteo_marine import OpenMeteoMarineProvider
from brizocast.providers.forecast.stormglass import StormglassProvider
from brizocast.providers.forecast.windy import WindyProvider

__all__ = [
    "DEFAULT_FORECAST_PROVIDER_KEY",
    "OpenMeteoMarineProvider",
    "StormglassProvider",
    "WindyProvider",
    "build_forecast_provider",
]
