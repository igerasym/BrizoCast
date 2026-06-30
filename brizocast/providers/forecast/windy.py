"""Windy forecast provider — pluggable stub.

:class:`WindyProvider` is a placeholder implementation of the
:class:`~brizocast.core.ports.forecast_provider.ForecastProvider` port. It is
selectable by its ``key`` via the provider factory (Req 6.3) and satisfies the
Protocol, but is not wired to the Windy API in the MVP. Calling
:meth:`get_forecast` raises :class:`~brizocast.core.errors.ProviderRequestError`
to signal that the provider is selectable but not yet configured (Req 6.5,
18.2).

Adding the real implementation later requires no changes to the scoring or
notification engines (Req 6.6).
"""

from __future__ import annotations

import httpx

from brizocast.core.domain.forecast import Forecast, ForecastWindow
from brizocast.core.errors import ProviderRequestError

__all__ = ["WindyProvider"]


class WindyProvider:
    """Pluggable Windy provider stub (not configured in the MVP)."""

    key: str = "windy"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def get_forecast(
        self, lat: float, lon: float, window: ForecastWindow
    ) -> Forecast:
        """Raise: the Windy provider is selectable but not configured."""

        raise ProviderRequestError(
            "Windy forecast provider is not configured in this build.",
            provider=self.key,
        )
