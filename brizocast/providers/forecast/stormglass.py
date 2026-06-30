"""Stormglass forecast provider — pluggable stub.

:class:`StormglassProvider` is a placeholder implementation of the
:class:`~brizocast.core.ports.forecast_provider.ForecastProvider` port. It is
selectable by its ``key`` via the provider factory (Req 6.3) and satisfies the
Protocol, but is not wired to the Stormglass API in the MVP. Calling
:meth:`get_forecast` raises :class:`~brizocast.core.errors.ProviderRequestError`
to signal that the provider is selectable but not yet configured, keeping raw
errors out of the business layer (Req 6.5, 18.2).

Adding the real implementation later requires no changes to the scoring or
notification engines (Req 6.6).
"""

from __future__ import annotations

import httpx

from brizocast.core.domain.forecast import Forecast, ForecastWindow
from brizocast.core.errors import ProviderRequestError

__all__ = ["StormglassProvider"]


class StormglassProvider:
    """Pluggable Stormglass provider stub (not configured in the MVP)."""

    key: str = "stormglass"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def get_forecast(
        self, lat: float, lon: float, window: ForecastWindow
    ) -> Forecast:
        """Raise: the Stormglass provider is selectable but not configured."""

        raise ProviderRequestError(
            "Stormglass forecast provider is not configured in this build.",
            provider=self.key,
        )
