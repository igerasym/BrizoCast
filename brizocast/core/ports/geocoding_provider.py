"""``GeocodingProvider`` port.

Abstract interface for resolving place and city names to candidate coordinates
(Req 2.3). The default implementation (Open-Meteo Geocoding) lives under
``brizocast/providers/geocoding`` and is bound to this port by the container, so
the geocoding source can be swapped without touching the location flow.

Import-light: depends only on the pure :class:`GeoCandidate` value object and
``typing``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from brizocast.core.domain.geo import GeoCandidate


@runtime_checkable
class GeocodingProvider(Protocol):
    """Resolves a free-text query to ranked geocoding candidates.

    :meth:`search` returns an empty list when no place matches (the caller asks
    the user for a new term, Req 2.6). A request failure is signalled by raising
    a domain provider error, surfaced to the user as temporarily unavailable
    (Req 2.11).
    """

    key: str

    async def search(self, query: str, limit: int = 5) -> list[GeoCandidate]:
        """Return up to ``limit`` candidate matches for ``query``."""
        ...
