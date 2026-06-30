"""SQLAlchemy implementation of :class:`CustomConditionRepository` (Req 4.5-4.7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from brizocast.models.custom_condition import CustomCondition
from brizocast.repositories.base import SqlAlchemyRepository

__all__ = ["SqlAlchemyCustomConditionRepository"]


class SqlAlchemyCustomConditionRepository(SqlAlchemyRepository[CustomCondition]):
    """Persists per-subscription custom condition overrides.

    Conforms structurally to
    :class:`brizocast.core.ports.repositories.CustomConditionRepository`.
    """

    model = CustomCondition

    async def add(self, condition: CustomCondition) -> CustomCondition:
        """Persist new custom conditions and return the stored entity."""
        return await self._add(condition)

    async def get_for_subscription(
        self, subscription_id: int
    ) -> CustomCondition | None:
        """Return the custom conditions for ``subscription_id``, or ``None``."""
        result = await self._session.execute(
            select(CustomCondition).where(
                CustomCondition.subscription_id == subscription_id
            )
        )
        return result.scalar_one_or_none()

    async def update(self, condition: CustomCondition) -> None:
        """Persist changes to existing (session-attached) custom conditions."""
        await self._flush()

    async def delete(self, subscription_id: int) -> None:
        """Remove the custom conditions bound to ``subscription_id`` (no-op if absent)."""
        condition = await self.get_for_subscription(subscription_id)
        if condition is not None:
            await self._delete_instance(condition)


if TYPE_CHECKING:
    from brizocast.core.ports.repositories import CustomConditionRepository

    def _assert_conforms(
        repo: SqlAlchemyCustomConditionRepository,
    ) -> CustomConditionRepository:
        """Static-only check that the implementation satisfies the port."""
        return repo
