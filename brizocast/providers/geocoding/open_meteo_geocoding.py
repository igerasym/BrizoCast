"""Open-Meteo Geocoding implementation of the :class:`GeocodingProvider` port.

``OpenMeteoGeocodingProvider`` resolves a free-text place or city name to ranked
:class:`~brizocast.core.domain.geo.GeoCandidate`s using the free Open-Meteo
Geocoding API (no API key required, Req 2.3). It is the default geocoding source
in the MVP and is bound to the :class:`GeocodingProvider` port by the container,
so the location flow never references this module directly.

Behaviour mandated by the requirements:

* When the API returns no matches, :meth:`search` returns an **empty list** so
  the caller can re-prompt the user for a new search term (Req 2.6).
* When the API call fails — a network/timeout error, a non-success HTTP status,
  or an unparseable / malformed payload — :meth:`search` raises
  :class:`~brizocast.core.errors.ProviderRequestError` tagged with this
  provider's key, which the boundary surfaces to the user as "temporarily
  unavailable" and logs (Req 2.11, 18.2).

The provider performs no I/O of its own beyond the injected
:class:`httpx.AsyncClient`; the client (and therefore its timeout, transport,
and lifecycle) is supplied by the composition root.
"""

from __future__ import annotations

from typing import Any, Final, TypeGuard

import httpx

from brizocast.core.domain.geo import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN, GeoCandidate
from brizocast.core.errors import ProviderRequestError
from brizocast.core.logging import get_logger

__all__ = ["OpenMeteoGeocodingProvider", "OPEN_METEO_GEOCODING_KEY"]

# Stable provider key, matched against ``Settings.GEOCODING_PROVIDER`` by the
# factory and used as structured log context.
OPEN_METEO_GEOCODING_KEY: Final[str] = "open_meteo_geocoding"

# Open-Meteo Geocoding search endpoint (no API key required).
_SEARCH_URL: Final[str] = "https://geocoding-api.open-meteo.com/v1/search"

# Default per-request timeout (seconds) used when the provider has to create its
# own client. An injected client keeps whatever timeout it was configured with.
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0

_log = get_logger(__name__, provider=OPEN_METEO_GEOCODING_KEY)


class OpenMeteoGeocodingProvider:
    """Resolve place/city names to coordinates via the Open-Meteo Geocoding API.

    Implements the :class:`~brizocast.core.ports.geocoding_provider.GeocodingProvider`
    port. An :class:`httpx.AsyncClient` is injected so the caller owns the
    client lifecycle, timeout, and transport; when none is supplied a private
    client with a sensible default timeout is created and closed per request.
    """

    key: str = OPEN_METEO_GEOCODING_KEY

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        """Initialise the provider.

        Args:
            client: Optional shared async HTTP client. When ``None`` the
                provider creates and closes a short-lived client (with a default
                timeout) for each :meth:`search` call.
        """
        self._client = client

    async def search(self, query: str, limit: int = 5) -> list[GeoCandidate]:
        """Return up to ``limit`` candidate matches for ``query``.

        Args:
            query: Free-text place or city name to resolve.
            limit: Maximum number of candidates to request (Open-Meteo ``count``).

        Returns:
            A list of :class:`GeoCandidate`, ordered by the API's relevance
            ranking. Empty when the API reports no matches (Req 2.6).

        Raises:
            ProviderRequestError: If the request fails at the network, HTTP, or
                payload-parsing level (Req 2.11, 18.2).
        """
        params = {
            "name": query,
            "count": limit,
            "format": "json",
        }

        payload = await self._get_json(params)
        return self._map_candidates(payload)

    # -- internals ------------------------------------------------------- #

    async def _get_json(self, params: dict[str, Any]) -> dict[str, Any]:
        """Perform the GET request and return the decoded JSON object.

        Wraps every failure mode (transport error, non-success status, invalid
        JSON) in :class:`ProviderRequestError` tagged with this provider's key.
        """
        try:
            if self._client is not None:
                response = await self._client.get(_SEARCH_URL, params=params)
            else:
                async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
                    response = await client.get(_SEARCH_URL, params=params)
            response.raise_for_status()
            data: Any = response.json()
        except ProviderRequestError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            # httpx.HTTPError covers transport, timeout, and status errors;
            # ValueError covers JSON decode failures.
            _log.warning("Open-Meteo geocoding request failed: %s", exc)
            raise ProviderRequestError(
                f"Open-Meteo geocoding request failed: {exc}",
                provider=self.key,
            ) from exc

        if not isinstance(data, dict):
            _log.warning("Open-Meteo geocoding returned a non-object payload")
            raise ProviderRequestError(
                "Open-Meteo geocoding returned an unexpected payload shape",
                provider=self.key,
            )
        return data

    def _map_candidates(self, payload: dict[str, Any]) -> list[GeoCandidate]:
        """Map a decoded Open-Meteo payload to a list of :class:`GeoCandidate`.

        A missing or empty ``results`` array yields an empty list (Req 2.6).
        Individual result entries that are malformed (missing name/coordinates,
        out-of-range coordinates, wrong types) raise
        :class:`ProviderRequestError` (Req 2.11, 18.2).
        """
        results = payload.get("results")
        if results is None:
            # No matches: the API omits ``results`` entirely for empty searches.
            return []
        if not isinstance(results, list):
            _log.warning("Open-Meteo geocoding 'results' was not a list")
            raise ProviderRequestError(
                "Open-Meteo geocoding payload had a non-list 'results' field",
                provider=self.key,
            )

        candidates: list[GeoCandidate] = []
        for entry in results:
            candidates.append(self._map_entry(entry))
        return candidates

    def _map_entry(self, entry: Any) -> GeoCandidate:
        """Map a single Open-Meteo result object to a :class:`GeoCandidate`."""
        if not isinstance(entry, dict):
            raise ProviderRequestError(
                "Open-Meteo geocoding result entry was not an object",
                provider=self.key,
            )

        name = entry.get("name")
        lat = entry.get("latitude")
        lon = entry.get("longitude")
        country = entry.get("country")

        if not isinstance(name, str) or not name:
            raise ProviderRequestError(
                "Open-Meteo geocoding result missing a valid 'name'",
                provider=self.key,
            )
        if not _is_real_number(lat) or not _is_real_number(lon):
            raise ProviderRequestError(
                "Open-Meteo geocoding result missing valid coordinates",
                provider=self.key,
            )

        lat_f = float(lat)
        lon_f = float(lon)
        if not (LAT_MIN <= lat_f <= LAT_MAX) or not (LON_MIN <= lon_f <= LON_MAX):
            raise ProviderRequestError(
                "Open-Meteo geocoding result coordinates out of range",
                provider=self.key,
            )

        country_str = country if isinstance(country, str) and country else None

        # The matched place name doubles as the city for a city/place search;
        # the country provides the wider context (Req 2.5).
        return GeoCandidate(
            name=name,
            lat=lat_f,
            lon=lon_f,
            city=name,
            country=country_str,
        )


def _is_real_number(value: Any) -> TypeGuard[int | float]:
    """Return ``True`` when ``value`` is a usable numeric coordinate.

    ``bool`` is rejected (it is a subclass of ``int`` but never a coordinate).
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)
