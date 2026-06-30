"""Surf spot value object (pure, no I/O).

Defines :class:`SurfSpot`, the framework-free domain representation of a
discoverable surf location (Req 5.1). It carries the stable ``spot_key``
identifier plus name and coordinates, and is what the :class:`SpotRepository`
port returns.

This domain value object is deliberately distinct from the SQLAlchemy ORM model
of the same name (``brizocast.models.surf_spot.SurfSpot``): the port and the
discovery logic depend only on this pure type, so the persistence/JSON backing
can change without touching business logic (Req 5.6). The two share field shape
but never import each other.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from brizocast.core.domain.geo import (
    LAT_MAX,
    LAT_MIN,
    LON_MAX,
    LON_MIN,
    GeoPoint,
)


class SurfSpot(BaseModel):
    """A discoverable surf location.

    ``spot_key`` is the stable identifier shared with the forecast cache,
    notification, and feedback records (it matches ``Forecast.spot_id``).
    Coordinates are validated with the same bounds as :class:`GeoPoint`;
    ``country`` and ``region`` are optional context.
    """

    model_config = ConfigDict(frozen=True)

    spot_key: str = Field(min_length=1, description="Stable unique identifier for the spot.")
    name: str = Field(min_length=1, description="Human-readable surf spot name.")
    lat: float = Field(ge=LAT_MIN, le=LAT_MAX, description="Latitude in degrees, -90..90.")
    lon: float = Field(ge=LON_MIN, le=LON_MAX, description="Longitude in degrees, -180..180.")
    country: str | None = Field(default=None, description="Country name, when known.")
    region: str | None = Field(default=None, description="Region name, when known.")

    def point(self) -> GeoPoint:
        """Return this spot's coordinates as a :class:`GeoPoint`."""

        return GeoPoint(lat=self.lat, lon=self.lon)
