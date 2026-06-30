"""Reverse geocoding: coordinates → country / region (OpenStreetMap Nominatim).

Surfline's ``mapview`` spot records carry only ``name`` + coordinates, not a
country/region breadcrumb. To enrich newly-imported spots with those display
fields we reverse-geocode the coordinates through OpenStreetMap's public
Nominatim service.

This is a polite, legitimate client:

* it sends an identifying ``User-Agent`` (Nominatim requires one),
* it self-limits to at most one request per ``min_interval`` seconds (Nominatim's
  usage policy asks for <= 1 req/s), and
* it degrades gracefully — any failure (network, non-200, parse, or a
  datacenter IP being blocked) yields ``(None, None)`` rather than raising, so
  spot ingestion is never blocked by missing metadata.

Attribution: results are derived from OpenStreetMap data (© OpenStreetMap
contributors, ODbL).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Final

import httpx

from brizocast.core.logging import BoundLogger, get_logger

__all__ = ["NominatimReverseGeocoder"]

_NOMINATIM_URL: Final = "https://nominatim.openstreetmap.org/reverse"
_DEFAULT_TIMEOUT_S: Final = 15.0
_DEFAULT_USER_AGENT: Final = "BrizoCast/0.1 (personal surf-alert bot)"
_DEFAULT_MIN_INTERVAL_S: Final = 1.0
# Language for returned place names (e.g. "Poland" rather than "Polska").
_DEFAULT_ACCEPT_LANGUAGE: Final = "en"
# Region falls back through these OSM address keys, most-specific first.
_REGION_KEYS: Final = ("state", "region", "county", "state_district")
# City falls back through these OSM address keys, most-specific first.
_CITY_KEYS: Final = ("city", "town", "village", "municipality", "suburb")


class NominatimReverseGeocoder:
    """Resolves ``(lat, lon)`` to ``(country, region)`` via Nominatim (graceful)."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        user_agent: str = _DEFAULT_USER_AGENT,
        timeout: float = _DEFAULT_TIMEOUT_S,
        min_interval_seconds: float = _DEFAULT_MIN_INTERVAL_S,
        accept_language: str = _DEFAULT_ACCEPT_LANGUAGE,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the reverse geocoder.

        Args:
            client: Optional shared :class:`httpx.AsyncClient` (reused, not
                closed). When ``None`` a short-lived client is used per call.
            user_agent: ``User-Agent`` sent with each request (Nominatim
                requires a meaningful one).
            timeout: Per-request timeout in seconds.
            min_interval_seconds: Minimum spacing between requests (politeness).
            accept_language: Language for returned place names, sent as the
                ``accept-language`` parameter so countries come back in a
                consistent language (e.g. "Poland" rather than "Polska").
            logger: Optional bound logger; one is created when omitted.
        """
        self._client = client
        self._user_agent = user_agent
        self._timeout = timeout
        self._min_interval = min_interval_seconds
        self._accept_language = accept_language
        self._log = logger or get_logger(__name__)
        self._last_call_monotonic = 0.0

    async def reverse(self, lat: float, lon: float) -> tuple[str | None, str | None]:
        """Return ``(country, region)`` for ``(lat, lon)``, or ``(None, None)``.

        Never raises: a failure (network, non-200, unparseable body, or a
        blocked IP) is logged at debug level and yields ``(None, None)`` so the
        caller can proceed without the metadata.
        """
        await self._respect_rate_limit()
        params: dict[str, Any] = {
            "lat": lat,
            "lon": lon,
            "format": "jsonv2",
            "zoom": 10,
            "addressdetails": 1,
            "accept-language": self._accept_language,
        }
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        try:
            response = await self._request(params, headers)
        except httpx.HTTPError as exc:
            self._log.debug("nominatim reverse failed for (%.4f, %.4f): %s", lat, lon, exc)
            return None, None

        if response.status_code != httpx.codes.OK:
            self._log.debug(
                "nominatim reverse HTTP %s for (%.4f, %.4f)",
                response.status_code,
                lat,
                lon,
            )
            return None, None
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            return None, None

        address = payload.get("address")
        if not isinstance(address, dict):
            return None, None
        country = address.get("country")
        region: str | None = None
        for key in _REGION_KEYS:
            value = address.get(key)
            if isinstance(value, str) and value:
                region = value
                break
        country = country if isinstance(country, str) and country else None
        return country, region

    async def reverse_full(
        self, lat: float, lon: float
    ) -> tuple[str | None, str | None]:
        """Return ``(city, country)`` for ``(lat, lon)``, or ``(None, None)``.

        Looks up city/town first (most specific), then falls back through
        village/municipality. Country is always included when available.
        """
        await self._respect_rate_limit()
        params: dict[str, Any] = {
            "lat": lat,
            "lon": lon,
            "format": "jsonv2",
            "zoom": 12,
            "addressdetails": 1,
            "accept-language": self._accept_language,
        }
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        try:
            response = await self._request(params, headers)
        except httpx.HTTPError as exc:
            self._log.debug("nominatim reverse_full failed for (%.4f, %.4f): %s", lat, lon, exc)
            return None, None
        if response.status_code != httpx.codes.OK:
            return None, None
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            return None, None
        address = payload.get("address")
        if not isinstance(address, dict):
            return None, None
        country = address.get("country")
        city: str | None = None
        for key in _CITY_KEYS:
            value = address.get(key)
            if isinstance(value, str) and value:
                city = value
                break
        country = country if isinstance(country, str) and country else None
        return city, country

    # -- internals ------------------------------------------------------- #

    async def _request(
        self, params: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        """Issue a single GET, reusing the shared client or a short-lived one."""
        if self._client is not None:
            return await self._client.get(
                _NOMINATIM_URL, params=params, headers=headers, timeout=self._timeout
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.get(_NOMINATIM_URL, params=params, headers=headers)

    async def _respect_rate_limit(self) -> None:
        """Sleep just enough to keep <= one request per ``min_interval`` seconds."""
        elapsed = time.monotonic() - self._last_call_monotonic
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_call_monotonic = time.monotonic()
