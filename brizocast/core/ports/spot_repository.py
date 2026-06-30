"""``SpotRepository`` port.

Abstract interface that supplies discoverable surf spots (Req 5.1) and exposes
radius-based discovery (Req 5.3). The MVP implementation is JSON-backed
(``JsonSpotRepository``) but the port lets the dataset be replaced by a database
without changing the discovery logic (Req 5.6).

The port returns the pure domain :class:`~brizocast.core.domain.spot.SurfSpot`
value object — never the SQLAlchemy ORM model — keeping the core free of
persistence concerns. Synchronous because the MVP backing is an in-memory JSON
load.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from brizocast.core.domain.geo import GeoPoint
from brizocast.core.domain.spot import SurfSpot


@runtime_checkable
class SpotRepository(Protocol):
    """Provides surf spots and radius-bounded discovery."""

    def all_spots(self) -> list[SurfSpot]:
        """Return every known surf spot."""
        ...

    def spots_within(self, center: GeoPoint, radius_km: float) -> list[SurfSpot]:
        """Return the spots within ``radius_km`` of ``center`` (inclusive)."""
        ...
