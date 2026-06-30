"""Surf-spot discovery service (Req 5.1, 5.2, 5.3, 5.5, 5.6).

``SpotDiscoveryService`` is the application-layer use case that, given a
subscription's center location and search radius, returns the surf spots within
that radius via the injected :class:`~brizocast.core.ports.spot_repository.SpotRepository`.

It depends only on the port (never a concrete repository), so the JSON backing
can be replaced by a database without touching this logic (Req 5.6). When no
spot lies within the radius it returns a clear "no nearby spots" result and logs
it, so the caller (the scheduler's forecast-check job, task 8.1) can skip
forecast collection for that subscription (Req 5.5).
"""

from __future__ import annotations

from dataclasses import dataclass

from brizocast.core.domain.geo import GeoPoint
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.core.ports.spot_repository import SpotRepository

__all__ = ["SpotDiscoveryResult", "SpotDiscoveryService"]


@dataclass(frozen=True)
class SpotDiscoveryResult:
    """Outcome of discovering spots for a subscription's location and radius.

    Carries the spots found within the radius along with the query context.
    ``has_nearby_spots`` is the explicit signal the scheduler uses to decide
    whether to collect forecasts: when it is ``False`` the subscription has no
    nearby spots and forecast collection is skipped (Req 5.5).
    """

    center: GeoPoint
    radius_km: float
    spots: tuple[SurfSpot, ...]
    subscription_id: int | None = None

    @property
    def has_nearby_spots(self) -> bool:
        """Whether at least one spot lies within the search radius."""
        return len(self.spots) > 0

    @property
    def is_empty(self) -> bool:
        """Whether discovery found no spots within the radius (the skip signal)."""
        return len(self.spots) == 0


class SpotDiscoveryService:
    """Discovers nearby surf spots for a subscription via a ``SpotRepository``."""

    def __init__(
        self,
        spot_repository: SpotRepository,
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            spot_repository: The port supplying spots and radius discovery.
            logger: Optional bound logger; one is created when omitted.
        """
        self._spots = spot_repository
        self._log = logger or get_logger(__name__)

    def discover(
        self,
        center: GeoPoint,
        radius_km: float,
        *,
        subscription_id: int | None = None,
    ) -> SpotDiscoveryResult:
        """Return the spots within ``radius_km`` of ``center`` (Req 5.3).

        When no spot is within the radius, the result is flagged empty and the
        outcome is logged with the subscription context so the caller can record
        the subscription as having no nearby spots and skip forecast collection
        (Req 5.5).

        Args:
            center: The subscription's location.
            radius_km: The subscription's search radius in kilometres.
            subscription_id: Optional subscription id for log context.

        Returns:
            A :class:`SpotDiscoveryResult` describing the discovered spots.
        """
        log = self._log.bind(subscription_id=subscription_id) if subscription_id is not None else self._log
        found = tuple(self._spots.spots_within(center, radius_km))
        result = SpotDiscoveryResult(
            center=center,
            radius_km=radius_km,
            spots=found,
            subscription_id=subscription_id,
        )
        if result.is_empty:
            log.info(
                "no nearby surf spots within %.1f km of (%.4f, %.4f); skipping forecast collection",
                radius_km,
                center.lat,
                center.lon,
            )
        else:
            log.debug(
                "discovered %d surf spot(s) within %.1f km",
                len(found),
                radius_km,
            )
        return result
