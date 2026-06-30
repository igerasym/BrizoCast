"""SQLAlchemy implementation of :class:`NotificationRepository` (Req 9.*, 16.3)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from brizocast.models.notification import NotificationSent
from brizocast.repositories.base import SqlAlchemyRepository

__all__ = ["SqlAlchemyNotificationRepository"]


class SqlAlchemyNotificationRepository(SqlAlchemyRepository[NotificationSent]):
    """Persists sent-notification records for anti-spam and digests.

    Conforms structurally to
    :class:`brizocast.core.ports.repositories.NotificationRepository`.
    """

    model = NotificationSent

    async def add(self, record: NotificationSent) -> NotificationSent:
        """Persist a new notification record and return the stored entity."""
        return await self._add(record)

    async def latest(
        self, subscription_id: int, spot_key: str, forecast_window_key: str
    ) -> NotificationSent | None:
        """Return the most recent record for the (sub, spot, window) identity.

        Ordered by ``sent_at`` then ``id`` descending so the newest record wins
        even when two records share a timestamp (Req 9.3-9.5).
        """
        result = await self._session.execute(
            select(NotificationSent)
            .where(
                NotificationSent.subscription_id == subscription_id,
                NotificationSent.spot_key == spot_key,
                NotificationSent.forecast_window_key == forecast_window_key,
            )
            .order_by(NotificationSent.sent_at.desc(), NotificationSent.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_since(
        self, subscription_id: int, since: datetime
    ) -> list[NotificationSent]:
        """Return records for ``subscription_id`` sent at/after ``since``."""
        result = await self._session.execute(
            select(NotificationSent)
            .where(
                NotificationSent.subscription_id == subscription_id,
                NotificationSent.sent_at >= since,
            )
            .order_by(NotificationSent.sent_at)
        )
        return list(result.scalars().all())


if TYPE_CHECKING:
    from brizocast.core.ports.repositories import NotificationRepository

    def _assert_conforms(
        repo: SqlAlchemyNotificationRepository,
    ) -> NotificationRepository:
        """Static-only check that the implementation satisfies the port."""
        return repo
