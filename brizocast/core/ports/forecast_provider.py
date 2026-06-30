"""``ForecastProvider`` port.

Abstract interface for retrieving a :class:`~brizocast.core.domain.forecast.Forecast`
for a geographic point and time window (Req 6.1). Concrete implementations
(Open-Meteo Marine default, Stormglass, Windy, …) live under
``brizocast/providers/forecast`` and are bound to this port by the container at
composition time, so forecast sources can be swapped without changing business
logic (Req 6.6).

This module is intentionally import-light: it depends only on pure domain value
objects and ``typing`` — no Telegram, SQLAlchemy, or HTTP code.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from brizocast.core.domain.forecast import Forecast, ForecastWindow


@runtime_checkable
class ForecastProvider(Protocol):
    """Retrieves forecasts for a coordinate over a forecast window.

    Implementations expose a stable ``key`` (used by the provider factory to
    resolve a configured selection) and an async :meth:`get_forecast` that maps
    a provider response to the domain :class:`Forecast` shape (Req 6.4).
    """

    key: str

    async def get_forecast(
        self, lat: float, lon: float, window: ForecastWindow
    ) -> Forecast:
        """Return the :class:`Forecast` for ``(lat, lon)`` over ``window``."""
        ...
