"""SQLAlchemy implementation of :class:`SubscriptionRepository` (Req 3.*, 16.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from brizocast.models.subscription import Subscription
from brizocast.repositories.base import SqlAlchemyRepository

__all__ = ["SqlAlchemySubscriptionRepository"]


class SqlAlchemySubscriptionRepository(SqlAlchemyRepository[Subscription]):
    """Persists user subscriptions.

    Conforms structurally to
    :class:`brizocast.core.ports.repositories.SubscriptionRepository`.
    """

    model = Subscription

    async def add(self, sub: Subscription) -> Subscription:
        """Persist a new subscription and return the stored entity."""
        return await self._add(sub)

    async def get(self, sub_id: int) -> Subscription | None:
        """Return the subscription with id ``sub_id``, or ``None``."""
        return await self._get_by_pk(sub_id)

    async def list_for_user(self, user_id: int) -> list[Subscription]:
        """Return every subscription owned by ``user_id``."""
        result = await self._session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.id)
        )
        return list(result.scalars().all())

    async def list_all_active(self) -> list[Subscription]:
        """Return every active subscription across all users."""
        result = await self._session.execute(
            select(Subscription)
            .where(Subscription.active.is_(True))
            .order_by(Subscription.id)
        )
        return list(result.scalars().all())

    async def update(self, sub: Subscription) -> None:
        """Persist changes to an existing (session-attached) subscription."""
        await self._flush()

    async def delete(self, sub_id: int) -> None:
        """Remove the subscription with id ``sub_id`` (no-op if absent)."""
        sub = await self._get_by_pk(sub_id)
        if sub is not None:
            await self._delete_instance(sub)

    async def count_for_user(self, user_id: int) -> int:
        """Return the number of subscriptions owned by ``user_id``."""
        result = await self._session.execute(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.user_id == user_id)
        )
        return int(result.scalar_one())


if TYPE_CHECKING:
    from brizocast.core.ports.repositories import SubscriptionRepository

    def _assert_conforms(
        repo: SqlAlchemySubscriptionRepository,
    ) -> SubscriptionRepository:
        """Static-only check that the implementation satisfies the port."""
        return repo
