"""``notifications_sent`` table — the anti-spam notification history."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base

if TYPE_CHECKING:
    from brizocast.models.subscription import Subscription
    from brizocast.models.surf_spot import SurfSpot


class NotificationSent(Base):
    """A record of a dispatched alert (Req 9.2).

    Queried by ``(subscription_id, spot_key, forecast_window_key)`` to find the
    latest record for anti-spam comparison (Req 9.3-9.5).
    """

    __tablename__ = "notifications_sent"
    __table_args__ = (
        Index(
            "ix_notifications_sent_dedup",
            "subscription_id",
            "spot_key",
            "forecast_window_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    spot_key: Mapped[str] = mapped_column(
        ForeignKey("surf_spots.spot_key"),
        nullable=False,
    )
    surf_score: Mapped[int] = mapped_column(Integer, nullable=False)
    forecast_window_key: Mapped[str] = mapped_column(String(64), nullable=False)
    forecast_window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    forecast_window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    subscription: Mapped[Subscription] = relationship(
        back_populates="notifications"
    )
    surf_spot: Mapped[SurfSpot] = relationship(back_populates="notifications")
