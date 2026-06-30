"""SQLAlchemy implementation of :class:`FeedbackRepository` (Req 12.4, 12.5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from brizocast.models.feedback import Feedback
from brizocast.repositories.base import SqlAlchemyRepository

__all__ = ["SqlAlchemyFeedbackRepository"]


class SqlAlchemyFeedbackRepository(SqlAlchemyRepository[Feedback]):
    """Persists user thumbs-up/down feedback on alerts.

    Conforms structurally to
    :class:`brizocast.core.ports.repositories.FeedbackRepository`.
    """

    model = Feedback

    async def add(self, feedback: Feedback) -> Feedback:
        """Persist a new feedback entry and return the stored entity."""
        return await self._add(feedback)

    async def list_for_subscription(self, subscription_id: int) -> list[Feedback]:
        """Return all feedback recorded for ``subscription_id``."""
        result = await self._session.execute(
            select(Feedback)
            .where(Feedback.subscription_id == subscription_id)
            .order_by(Feedback.id)
        )
        return list(result.scalars().all())


if TYPE_CHECKING:
    from brizocast.core.ports.repositories import FeedbackRepository

    def _assert_conforms(repo: SqlAlchemyFeedbackRepository) -> FeedbackRepository:
        """Static-only check that the implementation satisfies the port."""
        return repo
