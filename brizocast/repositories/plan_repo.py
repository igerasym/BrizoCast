"""SQLAlchemy implementation of :class:`PlanRepository` (Req 20.*, 16.3)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from brizocast.models.plan import Plan, PlanStatus, PlanTier
from brizocast.repositories.base import SqlAlchemyRepository

__all__ = ["SqlAlchemyPlanRepository"]


class SqlAlchemyPlanRepository(SqlAlchemyRepository[Plan]):
    """Persists the one-to-one monetization plan owned by each user.

    Conforms structurally to
    :class:`brizocast.core.ports.repositories.PlanRepository`.
    """

    model = Plan

    async def add(self, plan: Plan) -> Plan:
        """Persist a new plan and return the stored entity."""
        return await self._add(plan)

    async def get_for_user(self, user_id: int) -> Plan | None:
        """Return the plan owned by ``user_id``, or ``None``."""
        result = await self._session.execute(
            select(Plan).where(Plan.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def list_paid_active_expired(self, now: datetime) -> list[Plan]:
        """Return active Paid plans whose ``expiry_at`` is earlier than ``now``.

        Selects only plans that are still :attr:`PlanStatus.ACTIVE` on the
        :attr:`PlanTier.PAID` tier and carry a non-NULL ``expiry_at`` strictly
        before ``now`` — i.e. exactly the plans whose expiry has *passed* and
        that the plan-expiry check must transition to expired (Req 20.7). Free
        plans (``expiry_at IS NULL``) and already-expired/canceled plans are
        never returned, so the check is idempotent.
        """
        result = await self._session.execute(
            select(Plan).where(
                Plan.tier == PlanTier.PAID,
                Plan.status == PlanStatus.ACTIVE,
                Plan.expiry_at.is_not(None),
                Plan.expiry_at < now,
            )
        )
        return list(result.scalars().all())

    async def update(self, plan: Plan) -> None:
        """Persist changes to an existing (session-attached) plan."""
        await self._flush()


if TYPE_CHECKING:
    from brizocast.core.ports.repositories import PlanRepository

    def _assert_conforms(repo: SqlAlchemyPlanRepository) -> PlanRepository:
        """Static-only check that the implementation satisfies the port."""
        return repo
