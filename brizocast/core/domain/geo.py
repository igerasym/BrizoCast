"""Geographic value objects and pure geo math (no I/O).

Defines the immutable coordinate value objects shared across the domain and the
ports — :class:`GeoPoint` (a validated latitude/longitude pair) and
:class:`GeoCandidate` (a geocoding search result) — plus the pure great-circle
distance and discovery helpers (:func:`haversine_km`, :func:`spots_within`)
used by spot discovery.

Requirements covered: 6.4 (shared value-object shapes used by the forecast and
geocoding ports), 5.3 (discovery returns spots within radius), 5.4 (distance is
computed from latitude/longitude coordinates).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# Latitude/longitude bounds, expressed once so every coordinate-bearing value
# object validates consistently.
LAT_MIN = -90.0
LAT_MAX = 90.0
LON_MIN = -180.0
LON_MAX = 180.0

# Mean Earth radius in kilometres (IUGG mean radius R_1). Used as the sphere
# radius for great-circle distance.
EARTH_RADIUS_KM = 6371.0088


class GeoPoint(BaseModel):
    """An immutable geographic point.

    Latitude is constrained to ``[-90, 90]`` and longitude to ``[-180, 180]``;
    out-of-range values are rejected at construction time.
    """

    model_config = ConfigDict(frozen=True)

    lat: float = Field(ge=LAT_MIN, le=LAT_MAX, description="Latitude in degrees, -90..90.")
    lon: float = Field(ge=LON_MIN, le=LON_MAX, description="Longitude in degrees, -180..180.")


class GeoCandidate(BaseModel):
    """A geocoding search result returned by a ``GeocodingProvider``.

    Carries the matched place ``name`` and coordinates, plus optional ``city``
    and ``country`` context. Coordinates are validated with the same bounds as
    :class:`GeoPoint`.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, description="Human-readable place name of the match.")
    lat: float = Field(ge=LAT_MIN, le=LAT_MAX, description="Latitude in degrees, -90..90.")
    lon: float = Field(ge=LON_MIN, le=LON_MAX, description="Longitude in degrees, -180..180.")
    city: str | None = Field(default=None, description="City name, when known.")
    country: str | None = Field(default=None, description="Country name, when known.")

    def to_point(self) -> GeoPoint:
        """Return this candidate's coordinates as a :class:`GeoPoint`."""

        return GeoPoint(lat=self.lat, lon=self.lon)


@runtime_checkable
class HasCoordinates(Protocol):
    """Structural type for anything exposing ``lat``/``lon`` in degrees.

    Both :class:`GeoPoint` and the ``SurfSpot``/``Location`` ORM rows expose
    ``lat`` and ``lon`` floats, so discovery helpers can operate generically on
    domain value objects and persistence entities alike without coupling to a
    concrete class.
    """

    @property
    def lat(self) -> float: ...

    @property
    def lon(self) -> float: ...


# A spot type bound to the coordinate protocol so callers keep their concrete
# element type (e.g. ``SurfSpot``) through :func:`spots_within`.
S = TypeVar("S", bound=HasCoordinates)


def haversine_km(a: HasCoordinates, b: HasCoordinates) -> float:
    """Return the great-circle distance between ``a`` and ``b`` in kilometres.

    Uses the haversine formula on a sphere of radius
    :data:`EARTH_RADIUS_KM`. The result is:

    - **non-negative** for any inputs,
    - **exactly 0.0** for identical coordinates, and
    - **symmetric** — ``haversine_km(a, b) == haversine_km(b, a)``.

    Accepts any objects exposing ``lat``/``lon`` in degrees (a
    :class:`GeoPoint`, an ORM spot/location, etc.).
    """

    # Fast, exact path for identical coordinates: avoids floating-point noise
    # from the trig pipeline so identical points return precisely 0.0.
    if a.lat == b.lat and a.lon == b.lon:
        return 0.0

    lat1 = math.radians(a.lat)
    lat2 = math.radians(b.lat)
    d_lat = lat2 - lat1
    d_lon = math.radians(b.lon - a.lon)

    sin_half_lat = math.sin(d_lat / 2.0)
    sin_half_lon = math.sin(d_lon / 2.0)

    h = (
        sin_half_lat * sin_half_lat
        + math.cos(lat1) * math.cos(lat2) * sin_half_lon * sin_half_lon
    )
    # Clamp to [0, 1] to guard against tiny floating-point overshoot before
    # asin, then scale the central angle by the sphere radius.
    central_angle = 2.0 * math.asin(math.sqrt(min(1.0, max(0.0, h))))
    return EARTH_RADIUS_KM * central_angle


def spots_within(
    center: HasCoordinates,
    radius_km: float,
    spots: Iterable[S],
) -> list[S]:
    """Return the spots whose great-circle distance from ``center`` is within ``radius_km``.

    A spot is included exactly when its distance from ``center`` is **less than
    or equal to** ``radius_km`` (the boundary is inclusive, per Req 5.3). Input
    order is preserved and the original spot objects are returned unchanged, so
    the element type ``S`` is carried through.

    Pure and side-effect free: performs no I/O and does not mutate ``spots``.
    """

    return [spot for spot in spots if haversine_km(center, spot) <= radius_km]
