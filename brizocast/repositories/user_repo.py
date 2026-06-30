"""SQLAlchemy implementation of :class:`UserRepository` (Req 1.7, 16.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from brizocast.models.user import User
from brizocast.repositories.base import SqlAlchemyRepository

__all__ = ["SqlAlchemyUserRepository"]


class SqlAlchemyUserRepository(SqlAlchemyRepository[User]):
    """Persists users keyed by Telegram user id.

    Conforms structurally to
    :class:`brizocast.core.ports.repositories.UserRepository`.
    """

    model = User

    async def add(self, user: User) -> User:
        """Persist a new user and return the stored entity."""
        return await self._add(user)

    async def get(self, user_id: int) -> User | None:
        """Return the user with internal id ``user_id``, or ``None``."""
        return await self._get_by_pk(user_id)

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        """Return the user with the given Telegram id, or ``None``."""
        result = await self._session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        return result.scalar_one_or_none()

    async def update(self, user: User) -> None:
        """Persist changes to an existing (session-attached) user."""
        await self._flush()


if TYPE_CHECKING:
    from brizocast.core.ports.repositories import UserRepository

    def _assert_conforms(repo: SqlAlchemyUserRepository) -> UserRepository:
        """Static-only check that the implementation satisfies the port."""
        return repo
