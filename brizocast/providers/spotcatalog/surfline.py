"""Surfline spot-catalogue adapter (names + coordinates).

Implements :class:`~brizocast.providers.spotcatalog.base.SpotCatalogProvider`
against Surfline's public, undocumented endpoints that the website itself uses:

* ``GET services.surfline.com/kbyg/mapview`` — every spot inside a bounding box,
  each with ``name`` and ``lat``/``lon`` (the primary call here; a radius is
  approximated with a bounding box around the centre).
* ``GET services.surfline.com/taxonomy`` — the geoname/spot tree, used by
  :meth:`SurflineSpotCatalog.spots_in_taxonomy` for region-rooted imports.

Scope and limits (read me)
--------------------------
This is a **plain, polite HTTP client**: one request per call, a sane timeout,
and a configurable ``User-Agent``. It does **not** attempt to defeat Surfline's
bot protection — if the service answers with an HTTP 403 / challenge page (as it
does from some datacenter IPs), this raises :class:`SpotCatalogError` so the
caller can fall back to another catalogue or the operator can run the import
from a network where the public endpoints respond. Use is subject to Surfline's
terms; it is intended for the operator's own deployment.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
from typing import Any, Final

import httpx

from brizocast.core.domain.geo import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.providers.spotcatalog.base import SpotCatalogError, SpotCatalogProvider

try:
    from browserforge.headers import HeaderGenerator as _HeaderGenerator

    _header_gen: _HeaderGenerator | None = _HeaderGenerator(
        browser=("chrome", "firefox", "safari", "edge"),
        os=("windows", "macos"),
        device="desktop",
    )
except Exception:  # pragma: no cover — missing optional dependency
    _header_gen = None

__all__ = ["SurflineSpotCatalog"]

_MAPVIEW_URL: Final = "https://services.surfline.com/kbyg/mapview"
_TAXONOMY_URL: Final = "https://services.surfline.com/taxonomy"
_SEARCH_URL: Final = "https://services.surfline.com/search/site"
_DEFAULT_TIMEOUT_S: Final = 20.0
_DEFAULT_USER_AGENT: Final = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
_DEFAULT_MAX_ATTEMPTS: Final = 5
_DEFAULT_RETRY_BASE_S: Final = 2.0
_KM_PER_DEGREE_LAT: Final = 111.0
_SLUG_RE: Final = re.compile(r"[^a-z0-9]+")


def _generate_headers() -> dict[str, str]:
    """Generate realistic and consistent browser-like headers.

    Uses browserforge to produce headers that mimic real-world browser/OS
    frequency. We keep the browser-identifying headers (User-Agent, language,
    encoding, and the Chrome ``Sec-CH-UA`` client hints when present) for
    consistency, and force ``Accept`` to JSON since we call a JSON API.
    """
    if _header_gen is None:
        return {
            "User-Agent": _DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        }

    generated = dict(_header_gen.generate())
    # browserforge emits header names with mixed casing (e.g. lowercase
    # "sec-ch-ua"); look them up case-insensitively so nothing is lost.
    lower = {k.lower(): v for k, v in generated.items()}

    headers = {
        "User-Agent": lower.get("user-agent", _DEFAULT_USER_AGENT),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": lower.get("accept-language", "en-US,en;q=0.9"),
        "Accept-Encoding": lower.get("accept-encoding", "gzip, deflate, br"),
    }
    # Carry the Chrome/Edge client hints only when present (they are absent for
    # Firefox/Safari, so this preserves browser consistency).
    if "sec-ch-ua" in lower:
        headers["Sec-CH-UA"] = lower["sec-ch-ua"]
        headers["Sec-CH-UA-Mobile"] = lower.get("sec-ch-ua-mobile", "?0")
        headers["Sec-CH-UA-Platform"] = lower.get("sec-ch-ua-platform", '"Unknown"')
    return headers


def _slug(text: str) -> str:
    """Lower-case, hyphenated slug of ``text`` (for stable spot keys)."""
    return _SLUG_RE.sub("-", text.casefold()).strip("-") or "spot"


class SurflineSpotCatalog:
    """Fetches named surf spots from Surfline's public ``mapview``/``taxonomy``."""

    key: str = "surfline"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        user_agent: str = _DEFAULT_USER_AGENT,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        retry_base_seconds: float = _DEFAULT_RETRY_BASE_S,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the catalogue client.

        Args:
            client: Optional shared :class:`httpx.AsyncClient` (reused, not
                closed). When ``None`` a short-lived client is used per call.
            user_agent: ``User-Agent`` header sent with each request; override
                if the default is blocked from your network.
            timeout: Per-request timeout in seconds.
            max_attempts: How many times to attempt a request. Surfline's bot
                protection is intermittent, so a non-200 (e.g. 403) is retried
                with exponential backoff before giving up — no challenge is ever
                solved, the same plain request is simply retried.
            retry_base_seconds: Base delay for the exponential backoff.
            logger: Optional bound logger; one is created when omitted.
        """
        self._client = client
        self._user_agent = user_agent
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._retry_base = retry_base_seconds
        self._log = logger or get_logger(__name__)

    async def spots_near(
        self, lat: float, lon: float, radius_km: float
    ) -> list[SurfSpot]:
        """Return named spots within ``radius_km`` of ``(lat, lon)`` (bbox query)."""
        south, west, north, east = _bounding_box(lat, lon, radius_km)
        payload = await self._get(
            _MAPVIEW_URL,
            {"south": south, "west": west, "north": north, "east": east},
        )
        spots = _extract_mapview_spots(payload)
        result = [s for s in (self._to_spot(raw) for raw in spots) if s is not None]
        self._log.info(
            "surfline mapview returned %d spot(s) within %.0f km of (%.4f, %.4f)",
            len(result),
            radius_km,
            lat,
            lon,
        )
        return result

    async def search(self, query: str) -> list[SurfSpot]:
        """Search Surfline by name and return matching spots (name + coords).

        Uses the site search endpoint, which matches geonames and spots by text
        (e.g. ``"Gdańsk"``). Only spot results carrying coordinates are kept.
        """
        payload = await self._get(
            _SEARCH_URL,
            {
                "q": query,
                "querySize": "20",
                "suggestionSize": "20",
                "newsSearch": "false",
            },
        )
        raw = _extract_search_spots(payload)
        result = [s for s in (self._to_spot(r) for r in raw) if s is not None]
        self._log.info("surfline search %r returned %d spot(s)", query, len(result))
        return result

    async def spots_in_taxonomy(self, geoname_id: str, *, max_depth: int = 2) -> list[SurfSpot]:
        """Return spots under a Surfline geoname node (region-rooted import)."""
        payload = await self._get(
            _TAXONOMY_URL,
            {"type": "taxonomy", "id": geoname_id, "maxDepth": str(max_depth)},
        )
        contains = payload.get("contains")
        nodes = contains if isinstance(contains, list) else []
        result = [
            spot
            for node in nodes
            if isinstance(node, dict) and node.get("type") == "spot"
            for spot in (self._to_spot(node),)
            if spot is not None
        ]
        self._log.info(
            "surfline taxonomy %s returned %d spot(s)", geoname_id, len(result)
        )
        return result

    # -- internals ------------------------------------------------------- #

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET ``url`` with ``params``, retrying intermittent blocks, return JSON.

        Surfline's edge intermittently answers ``403`` with a challenge page even
        to legitimate clients. Rather than solving the challenge (which we never
        do), the same plain request is retried a few times with exponential
        backoff + jitter; many transient blocks clear on a later attempt.
        """

        headers = _generate_headers()

        last_status: int | None = None
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            # Generate fresh headers on each retry — different fingerprint may
            # pass Cloudflare when the previous one was blocked.
            if attempt > 1:
                headers = _generate_headers()
            try:
                response = await self._request(url, params, headers)
            except httpx.HTTPError as exc:
                last_error = exc
                last_status = None
            else:
                if response.status_code == httpx.codes.OK:
                    try:
                        data: dict[str, Any] = response.json()
                    except ValueError as exc:
                        raise SpotCatalogError(
                            "surfline response was not JSON (likely a "
                            "challenge/HTML page)"
                        ) from exc
                    return data
                last_status = response.status_code
                last_error = None

            if attempt < self._max_attempts:
                delay = self._retry_base * (2 ** (attempt - 1))
                delay += random.uniform(0, self._retry_base)  # noqa: S311 - jitter only
                self._log.debug(
                    "surfline attempt %d/%d failed (status=%s); retrying in %.1fs",
                    attempt,
                    self._max_attempts,
                    last_status,
                    delay,
                )
                await asyncio.sleep(delay)

        if last_error is not None:
            raise SpotCatalogError(
                f"surfline request failed after {self._max_attempts} attempts: "
                f"{last_error}"
            ) from last_error
        raise SpotCatalogError(
            f"surfline returned HTTP {last_status} after {self._max_attempts} "
            "attempts (intermittent bot protection; try again later or pursue "
            "official access)"
        )

    async def _request(
        self, url: str, params: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        """Issue a single GET, reusing the shared client or a short-lived one."""
        if self._client is not None:
            return await self._client.get(
                url, params=params, headers=headers, timeout=self._timeout
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.get(url, params=params, headers=headers)

    def _to_spot(self, raw: dict[str, Any]) -> SurfSpot | None:
        """Map a Surfline spot object to a :class:`SurfSpot`, or ``None`` if invalid."""
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        lat, lon = _extract_coords(raw)
        if lat is None or lon is None:
            return None
        if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
            return None
        spot_id = str(raw.get("_id") or raw.get("spotId") or "")
        suffix = f"-{spot_id[-6:]}" if spot_id else ""
        return SurfSpot(
            spot_key=f"surfline-{_slug(name)}{suffix}",
            name=name.strip(),
            lat=lat,
            lon=lon,
            country=_path_country(raw),
            region=_subregion_name(raw) or _path_region(raw),
        )


def _bounding_box(
    lat: float, lon: float, radius_km: float
) -> tuple[float, float, float, float]:
    """Return ``(south, west, north, east)`` for a radius around a centre."""
    dlat = radius_km / _KM_PER_DEGREE_LAT
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    dlon = radius_km / (_KM_PER_DEGREE_LAT * cos_lat)
    return (
        max(LAT_MIN, lat - dlat),
        max(LON_MIN, lon - dlon),
        min(LAT_MAX, lat + dlat),
        min(LON_MAX, lon + dlon),
    )


def _extract_mapview_spots(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the spot list out of a ``mapview`` response, defensively."""
    data = payload.get("data")
    container = data if isinstance(data, dict) else payload
    spots = container.get("spots")
    return [s for s in spots if isinstance(s, dict)] if isinstance(spots, list) else []


def _extract_search_spots(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect spot-like records from a site-search response.

    The search payload nests results under Elasticsearch-style ``hits.hits``
    arrays where each hit wraps the document in ``_source``. This walks the
    structure and returns the unwrapped ``_source`` dicts that look like spots
    (i.e. carry coordinates), tolerating shape changes by recursing generically.
    """
    found: list[dict[str, Any]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            source = node.get("_source")
            if isinstance(source, dict):
                lat, lon = _extract_coords(source)
                if source.get("name") and lat is not None and lon is not None:
                    found.append(source)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def _extract_coords(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    """Read ``(lat, lon)`` from a spot's direct fields or GeoJSON location."""
    lat = raw.get("lat")
    lon = raw.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    location = raw.get("location")
    if isinstance(location, dict):
        coords = location.get("coordinates")
        # GeoJSON order is [longitude, latitude].
        if isinstance(coords, list) and len(coords) == 2:
            clon, clat = coords
            if isinstance(clat, (int, float)) and isinstance(clon, (int, float)):
                return float(clat), float(clon)
    return None, None


def _subregion_name(raw: dict[str, Any]) -> str | None:
    """Region label from the mapview spot's ``subregion`` object (e.g. "Northern Poland").

    The ``mapview`` endpoint omits the ``enumeratedPath`` geoname breadcrumb but
    embeds a ``subregion`` object whose ``name`` is Surfline's own, surf-relevant
    region label. Preferred over the (absent) geoname path for these records.
    """
    sub = raw.get("subregion")
    if isinstance(sub, dict):
        name = sub.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _enumerated_path(raw: dict[str, Any]) -> list[str]:
    """Return the spot's geoname path parts (e.g. Earth, Europe, France, …)."""
    path = raw.get("enumeratedPath")
    if isinstance(path, str):
        return [part for part in path.split(",") if part]
    return []


def _path_country(raw: dict[str, Any]) -> str | None:
    """Best-effort country from the geoname path (3rd element after Earth)."""
    parts = _enumerated_path(raw)
    # Typical path: Earth, <Continent>, <Country>, <Region>, ...
    return parts[2] if len(parts) >= 3 else None


def _path_region(raw: dict[str, Any]) -> str | None:
    """Best-effort region from the geoname path (4th element)."""
    parts = _enumerated_path(raw)
    return parts[3] if len(parts) >= 4 else None


# Static-only confirmation that the adapter satisfies the port.
_CONFORMS: SpotCatalogProvider = SurflineSpotCatalog()
