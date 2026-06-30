"""Location management service (Req 2.2, 2.5, 2.7, 2.8, 2.9, 2.10).

``LocationService`` is the application-layer use case for creating and managing
a user's :class:`~brizocast.models.location.Location` rows and their saved
Favorite_Locations. It composes the
:class:`~brizocast.repositories.location_repo.SqlAlchemyLocationRepository` over
a caller-independent unit of work and, optionally, a
:class:`~brizocast.core.ports.geocoding_provider.GeocodingProvider` so the bot's
location flow can resolve a free-text search to candidates through the same
service.

Responsibilities:

* Create a location from a shared Telegram point (Req 2.2,
  :meth:`create_from_coordinates`).
* Create a location from a selected geocoding candidate, carrying its city and
  country (Req 2.5, :meth:`create_from_candidate`).
* Save (mark) a location as a favorite (Req 2.7), list a user's favorites with
  their label and place name (Req 2.8, 2.9), and delete a favorite (Req 2.10).
* Optionally pass a free-text query through to an injected geocoding provider
  (Req 2.3) so the handler can search and then call
  :meth:`create_from_candidate`.

Unit-of-work strategy
---------------------
The service is injected with an ``async_sessionmaker`` (not a live session) and
opens a fresh transactional session via
:func:`brizocast.database.session.session_scope` for **each** public operation,
constructing a thin ``SqlAlchemyLocationRepository`` bound to that session. The
``session_scope`` context manager owns the transaction (commit on success,
rollback on error, always close), matching the repository's caller-owns-the-unit
-of-work contract. Because the session factory is built with
``expire_on_commit=False``, the returned ORM entities remain readable after the
session closes.

Geocoding search itself lives in the provider; this service only persists
locations and favorites and offers a thin :meth:`search` passthrough.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.core.domain.geo import GeoCandidate
from brizocast.core.errors import NotFoundError
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.core.ports.geocoding_provider import GeocodingProvider
from brizocast.database.session import session_scope
from brizocast.models.location import Location
from brizocast.providers.geocoding.reverse import NominatimReverseGeocoder
from brizocast.repositories.location_repo import SqlAlchemyLocationRepository

__all__ = ["LocationService"]


class LocationService:
    """Create and manage user locations and favorites (Req 2.*)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        geocoding_provider: GeocodingProvider | None = None,
        reverse_geocoder: NominatimReverseGeocoder | None = None,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            session_factory: Async session factory used to open one
                transactional unit of work per operation via
                :func:`session_scope`.
            geocoding_provider: Optional geocoding provider for the
                :meth:`search` passthrough; not required for persistence.
            reverse_geocoder: Optional reverse geocoder used to enrich
                shared GPS coordinates with a human-readable city and country.
            logger: Optional bound logger; one is created when omitted.
        """
        self._session_factory = session_factory
        self._geocoding = geocoding_provider
        self._reverse_geocoder = reverse_geocoder
        self._log = logger or get_logger(__name__)

    # -- creation -------------------------------------------------------- #

    async def create_from_coordinates(
        self,
        user_id: int,
        lat: float,
        lon: float,
        *,
        label: str | None = None,
        is_favorite: bool = False,
    ) -> Location:
        """Create a location from a shared Telegram point (Req 2.2).

        Args:
            user_id: Owning user's internal id.
            lat: Latitude in degrees.
            lon: Longitude in degrees.
            label: Optional user-facing label.
            is_favorite: When ``True``, the location is saved as a favorite
                immediately (Req 2.7).

        Returns:
            The persisted :class:`Location` with its assigned id.
        """
        # Reverse-geocode coords → city + country via Nominatim.
        city: str | None = None
        country: str | None = None
        if self._reverse_geocoder is not None:
            city, country = await self._reverse_geocoder.reverse_full(lat, lon)

        location = Location(
            user_id=user_id,
            lat=lat,
            lon=lon,
            label=label,
            city=city,
            country=country,
            is_favorite=is_favorite,
        )
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyLocationRepository(session, logger=self._log)
            stored = await repo.add(location)
        self._log.bind(user_id=user_id, location_id=stored.id).info(
            "created location from shared coordinates (favorite=%s)", is_favorite
        )
        return stored

    async def create_from_candidate(
        self,
        user_id: int,
        candidate: GeoCandidate,
        *,
        label: str | None = None,
        is_favorite: bool = False,
    ) -> Location:
        """Create a location from a selected geocoding candidate (Req 2.5).

        Carries the candidate's latitude, longitude, city, and country. When no
        explicit ``label`` is given, the candidate's place ``name`` is used so a
        saved favorite always lists a meaningful place name (Req 2.9).

        Args:
            user_id: Owning user's internal id.
            candidate: The geocoding candidate the user selected.
            label: Optional user-facing label; defaults to ``candidate.name``.
            is_favorite: When ``True``, the location is saved as a favorite
                immediately (Req 2.7).

        Returns:
            The persisted :class:`Location` with its assigned id.
        """
        location = Location(
            user_id=user_id,
            lat=candidate.lat,
            lon=candidate.lon,
            label=label if label is not None else candidate.name,
            city=candidate.city,
            country=candidate.country,
            is_favorite=is_favorite,
        )
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyLocationRepository(session, logger=self._log)
            stored = await repo.add(location)
        self._log.bind(user_id=user_id, location_id=stored.id).info(
            "created location from geocoding candidate %r (favorite=%s)",
            candidate.name,
            is_favorite,
        )
        return stored

    # -- favorites ------------------------------------------------------- #

    async def save_favorite(self, location_id: int) -> Location:
        """Mark an existing location as a saved favorite (Req 2.7).

        Args:
            location_id: Id of the location to flag as a favorite.

        Returns:
            The updated :class:`Location`.

        Raises:
            NotFoundError: If no location with ``location_id`` exists.
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyLocationRepository(session, logger=self._log)
            location = await repo.get(location_id)
            if location is None:
                raise NotFoundError(f"location {location_id} not found")
            location.is_favorite = True
            # The entity is attached to this session; the enclosing
            # ``session_scope`` commits the UPDATE on exit.
        self._log.bind(location_id=location_id).info("saved location as favorite")
        return location

    async def list_favorites(self, user_id: int) -> list[Location]:
        """Return the user's saved favorites with label and place (Req 2.8, 2.9).

        Args:
            user_id: Owning user's internal id.

        Returns:
            Every favorite location owned by ``user_id`` (possibly empty).
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyLocationRepository(session, logger=self._log)
            return await repo.list_favorites(user_id)

    async def list_locations(self, user_id: int) -> list[Location]:
        """Return every location owned by ``user_id`` (favorites and transient)."""
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyLocationRepository(session, logger=self._log)
            return await repo.list_for_user(user_id)

    async def delete_favorite(self, location_id: int) -> None:
        """Delete a saved favorite location (Req 2.10).

        Removes exactly the selected location, leaving the user's other
        favorites intact. Idempotent: deleting an absent location is a no-op.
        """
        await self.delete_location(location_id)

    async def delete_location(self, location_id: int) -> None:
        """Remove the location with id ``location_id`` (Req 2.10).

        Idempotent: a no-op when no such location exists.
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyLocationRepository(session, logger=self._log)
            await repo.delete(location_id)
        self._log.bind(location_id=location_id).info("deleted location")

    # -- geocoding passthrough ------------------------------------------ #

    async def search(self, query: str, limit: int = 5) -> list[GeoCandidate]:
        """Resolve a free-text place ``query`` to candidates (Req 2.3).

        Thin passthrough to the injected geocoding provider; the persistence of
        a chosen candidate is done separately via :meth:`create_from_candidate`.

        Args:
            query: The place or city name to search for.
            limit: Maximum number of candidates to return.

        Returns:
            The provider's ranked candidates (empty when nothing matches).

        Raises:
            NotFoundError: If no geocoding provider was injected.
        """
        if self._geocoding is None:
            raise NotFoundError("no geocoding provider configured for search")
        return await self._geocoding.search(query, limit)
