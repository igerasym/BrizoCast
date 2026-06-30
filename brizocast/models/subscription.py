"""``subscriptions`` table — binds a user, activity, location, and preferences."""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from brizocast.models.activity import Activity
    from brizocast.models.custom_condition import CustomCondition
    from brizocast.models.feedback import Feedback
    from brizocast.models.location import Location
    from brizocast.models.notification import NotificationSent
    from brizocast.models.preset import Preset
    from brizocast.models.user import User


# Default search radius in kilometers when the user does not specify one
# (Req 3.2). The valid range [1, 200] is enforced at the service layer (Req 3.9).
DEFAULT_SEARCH_RADIUS_KM: float = 20.0


class Subscription(CreatedAtMixin, Base):
    """A user-owned monitoring configuration.

    Associated with exactly one user, one activity, and one location
    (Req 16.6). May reference a preset (``preset_id`` nullable) or be overridden
    by a one-to-one :class:`~brizocast.models.custom_condition.CustomCondition`.
    Notification preferences (mode, quiet hours, mute, snooze) live here
    (Req 10.2, 11.1).
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id"),
        index=True,
        nullable=False,
    )
    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id"),
        index=True,
        nullable=False,
    )
    search_radius_km: Mapped[float] = mapped_column(
        Float, default=DEFAULT_SEARCH_RADIUS_KM, nullable=False
    )
    preset_id: Mapped[int | None] = mapped_column(
        ForeignKey("presets.id"),
        nullable=True,
    )
    notification_mode: Mapped[str] = mapped_column(
        String(32), default="immediate", nullable=False
    )
    quiet_hours_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    muted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    snooze_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="subscriptions")
    activity: Mapped[Activity] = relationship(back_populates="subscriptions")
    location: Mapped[Location] = relationship(back_populates="subscriptions")
    preset: Mapped[Preset | None] = relationship(back_populates="subscriptions")
    # One-to-one optional override (Req 4.7).
    custom_conditions: Mapped[CustomCondition | None] = relationship(
        back_populates="subscription",
        uselist=False,
        cascade="all, delete-orphan",
    )
    notifications: Mapped[list[NotificationSent]] = relationship(
        back_populates="subscription",
        cascade="all, delete-orphan",
    )
    feedback: Mapped[list[Feedback]] = relationship(
        back_populates="subscription",
        cascade="all, delete-orphan",
    )
