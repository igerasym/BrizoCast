"""SQLAlchemy implementation of :class:`LocationRepository` (Req 2.*, 16.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from brizocast.models.location import Location
from brizocast.repositories.base import SqlAlchemyRepository

__all__ = ["SqlAlchemyLocationRepository"]


class SqlAlchemyLocationRepository(SqlAlchemyRepository[Location]):
    """Persists user locations and saved favorites.

    Conforms structurally to
    :class:`brizocast.core.ports.repositories.LocationRepository`.
    """

    model = Location

    async def add(self, location: Location) -> Location:
        """Persist a new location and return the stored entity."""
        return await self._add(location)

    async def get(self, location_id: int) -> Location | None:
        """Return the location with id ``location_id``, or ``None``."""
        return await self._get_by_pk(location_id)

    async def list_for_user(self, user_id: int) -> list[Location]:
        """Return every location owned by ``user_id``."""
        result = await self._session.execute(
            select(Location)
            .where(Location.user_id == user_id)
            .order_by(Location.id)
        )
        return list(result.scalars().all())

    async def list_favorites(self, user_id: int) -> list[Location]:
        """Return the user's saved favorite locations."""
        result = await self._session.execute(
            select(Location)
            .where(Location.user_id == user_id, Location.is_favorite.is_(True))
            .order_by(Location.id)
        )
        return list(result.scalars().all())

    async def delete(self, location_id: int) -> None:
        """Remove the location with id ``location_id`` (no-op if absent)."""
        location = await self._get_by_pk(location_id)
        if location is not None:
            await self._delete_instance(location)


if TYPE_CHECKING:
    from brizocast.core.ports.repositories import LocationRepository

    def _assert_conforms(repo: SqlAlchemyLocationRepository) -> LocationRepository:
        """Static-only check that the implementation satisfies the port."""
        return repo
