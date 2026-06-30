"""SQLAlchemy implementation of :class:`PaymentRepository` (Req 16.8, 16.3).

Reserved for future payment integration; not populated while monetization is
disabled, but the persistence surface exists from day one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from brizocast.models.payment import PaymentRecord
from brizocast.repositories.base import SqlAlchemyRepository

__all__ = ["SqlAlchemyPaymentRepository"]


class SqlAlchemyPaymentRepository(SqlAlchemyRepository[PaymentRecord]):
    """Persists billing transactions associated with a user's plan.

    Conforms structurally to
    :class:`brizocast.core.ports.repositories.PaymentRepository`.
    """

    model = PaymentRecord

    async def add(self, payment: PaymentRecord) -> PaymentRecord:
        """Persist a new payment record and return the stored entity."""
        return await self._add(payment)

    async def list_for_plan(self, plan_id: int) -> list[PaymentRecord]:
        """Return all payment records associated with ``plan_id``."""
        result = await self._session.execute(
            select(PaymentRecord)
            .where(PaymentRecord.plan_id == plan_id)
            .order_by(PaymentRecord.id)
        )
        return list(result.scalars().all())


if TYPE_CHECKING:
    from brizocast.core.ports.repositories import PaymentRepository

    def _assert_conforms(repo: SqlAlchemyPaymentRepository) -> PaymentRepository:
        """Static-only check that the implementation satisfies the port."""
        return repo
