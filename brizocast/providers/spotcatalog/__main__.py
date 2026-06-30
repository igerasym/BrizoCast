"""Manual connectivity test for the Surfline spot catalogue.

Run from a network where Surfline's public endpoints respond (e.g. the
Raspberry Pi on a home connection)::

    python -m brizocast.providers.spotcatalog 54.40 18.57 60
    python -m brizocast.providers.spotcatalog 43.66 -1.44 40

It prints the named spots Surfline returns within the radius, or a clear error
if the request was blocked. A custom User-Agent can be supplied as a 4th arg.
"""

from __future__ import annotations

import asyncio
import sys

from brizocast.providers.spotcatalog.base import SpotCatalogError
from brizocast.providers.spotcatalog.surfline import SurflineSpotCatalog


async def _run(lat: float, lon: float, radius_km: float, user_agent: str | None) -> int:
    """Query Surfline by radius and print the discovered spots; return exit code."""
    catalog = (
        SurflineSpotCatalog(user_agent=user_agent)
        if user_agent
        else SurflineSpotCatalog()
    )
    try:
        spots = await catalog.spots_near(lat, lon, radius_km)
    except SpotCatalogError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"Surfline returned {len(spots)} spot(s) within {radius_km:g} km:")
    for spot in sorted(spots, key=lambda s: s.name.casefold()):
        place = ", ".join(p for p in (spot.region, spot.country) if p)
        suffix = f"  [{place}]" if place else ""
        print(f"  {spot.name}  ({spot.lat:.4f}, {spot.lon:.4f}){suffix}")
    return 0


async def _run_search(query: str, user_agent: str | None) -> int:
    """Search Surfline by name and print matching spots; return exit code."""
    catalog = (
        SurflineSpotCatalog(user_agent=user_agent)
        if user_agent
        else SurflineSpotCatalog()
    )
    try:
        spots = await catalog.search(query)
    except SpotCatalogError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"Surfline search {query!r} returned {len(spots)} spot(s):")
    for spot in sorted(spots, key=lambda s: s.name.casefold()):
        place = ", ".join(p for p in (spot.region, spot.country) if p)
        suffix = f"  [{place}]" if place else ""
        print(f"  {spot.name}  ({spot.lat:.4f}, {spot.lon:.4f}){suffix}")
    return 0


async def _run_raw(lat: float, lon: float, radius_km: float, user_agent: str | None) -> int:
    """Dump the raw Surfline mapview JSON for the first spot in the radius.

    Lets us inspect exactly which fields the ``mapview`` response carries for a
    spot (e.g. whether ``country``/``region``/``enumeratedPath`` are present),
    rather than guessing from the parsed result.
    """
    import json
    import math

    import httpx

    from brizocast.providers.spotcatalog.surfline import (
        _MAPVIEW_URL,
        _extract_mapview_spots,
        _generate_headers,
    )

    km = 111.0
    dlat = radius_km / km
    dlon = radius_km / (km * max(math.cos(math.radians(lat)), 0.01))
    params = {
        "south": lat - dlat,
        "west": lon - dlon,
        "north": lat + dlat,
        "east": lon + dlon,
    }
    headers = _generate_headers()
    if user_agent:
        headers["User-Agent"] = user_agent
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(_MAPVIEW_URL, params=params, headers=headers)
    print(f"HTTP {response.status_code}")
    if response.status_code != httpx.codes.OK:
        print(response.text[:400])
        return 1
    payload = response.json()
    spots = _extract_mapview_spots(payload)
    print(f"{len(spots)} spot(s); top-level data keys: {sorted(payload.keys())}")
    if spots:
        print("first spot's keys:", sorted(spots[0].keys()))
        print(json.dumps(spots[0], indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    """Parse argv and run a radius query, name search, or raw dump.

    Usage:
        python -m brizocast.providers.spotcatalog <lat> <lon> <radius_km> [ua]
        python -m brizocast.providers.spotcatalog search "<query>" [ua]
        python -m brizocast.providers.spotcatalog raw <lat> <lon> <radius_km> [ua]
    """
    args = sys.argv[1:]
    if len(args) >= 4 and args[0] == "raw":
        raw_ua = args[4] if len(args) > 4 else None
        raise SystemExit(
            asyncio.run(
                _run_raw(float(args[1]), float(args[2]), float(args[3]), raw_ua)
            )
        )
    if len(args) >= 2 and args[0] == "search":
        query = args[1]
        search_ua = args[2] if len(args) > 2 else None
        raise SystemExit(asyncio.run(_run_search(query, search_ua)))
    if len(args) < 3:
        print(
            "usage:\n"
            "  python -m brizocast.providers.spotcatalog <lat> <lon> <radius_km> [user_agent]\n"
            '  python -m brizocast.providers.spotcatalog search "<query>" [user_agent]\n'
            "  python -m brizocast.providers.spotcatalog raw <lat> <lon> <radius_km> [user_agent]"
        )
        raise SystemExit(2)
    lat = float(args[0])
    lon = float(args[1])
    radius_km = float(args[2])
    user_agent = args[3] if len(args) > 3 else None
    raise SystemExit(asyncio.run(_run(lat, lon, radius_km, user_agent)))


if __name__ == "__main__":
    main()
