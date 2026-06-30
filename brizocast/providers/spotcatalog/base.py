"""``SpotCatalogProvider`` port and shared errors.

A spot catalogue answers "which named surf spots are in this area?" returning
pure :class:`~brizocast.core.domain.spot.SurfSpot` value objects (name +
coordinates, optional country/region). Concrete adapters (Surfline, OSM, …)
bind to this port so the import flow and admin UI depend only on the
abstraction and the data source can be swapped freely.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from brizocast.core.domain.spot import SurfSpot
from brizocast.core.errors import BrizoCastError

__all__ = ["SpotCatalogError", "SpotCatalogProvider"]


class SpotCatalogError(BrizoCastError):
    """Raised when a spot catalogue source is unreachable or returns bad data.

    Examples: the source blocked the request (e.g. bot protection / HTTP 403),
    the network call failed, or the response could not be parsed into spots.
    Surfaced loudly so the caller can fall back to another catalogue or report
    the failure to the operator rather than silently importing nothing.
    """


@runtime_checkable
class SpotCatalogProvider(Protocol):
    """Supplies named surf spots within an area (Req: provider-sourced spots)."""

    key: str

    async def spots_near(
        self, lat: float, lon: float, radius_km: float
    ) -> list[SurfSpot]:
        """Return the named surf spots within ``radius_km`` of ``(lat, lon)``.

        Implementations may approximate the radius with a bounding box. They
        raise :class:`SpotCatalogError` on a transport/parse failure rather than
        returning a partial or empty result silently.
        """
        ...
