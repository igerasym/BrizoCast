"""``feedback`` table — user thumbs-up/down responses to alerts."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from brizocast.models.subscription import Subscription
    from brizocast.models.surf_spot import SurfSpot


class FeedbackRating(StrEnum):
    """A user's rating of an alert (Req 12.3, 12.4)."""

    UP = "up"
    DOWN = "down"


class Feedback(CreatedAtMixin, Base):
    """Persisted feedback associated with a subscription, spot, and score.

    Retained for preset tuning and future scoring use (Req 12.4, 12.5).
    """

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    spot_key: Mapped[str] = mapped_column(
        ForeignKey("surf_spots.spot_key"),
        nullable=False,
    )
    surf_score: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[FeedbackRating] = mapped_column(
        Enum(
            FeedbackRating,
            native_enum=False,
            length=8,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )

    subscription: Mapped[Subscription] = relationship(back_populates="feedback")
    surf_spot: Mapped[SurfSpot] = relationship(back_populates="feedback")
