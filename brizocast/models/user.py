"""``users`` table — a Telegram user and the root of most ownership chains."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from brizocast.models.location import Location
    from brizocast.models.plan import Plan
    from brizocast.models.preset import Preset
    from brizocast.models.subscription import Subscription


class User(CreatedAtMixin, Base):
    """A Telegram user, keyed by the unique Telegram user identifier.

    A user owns exactly one :class:`~brizocast.models.plan.Plan`
    (one-to-one), and may own many locations, subscriptions, and custom
    presets (Req 16.6, 16.9, 20.1).
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    selected_activity_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # One-to-one: every user has exactly one plan (Req 20.1).
    plan: Mapped[Plan] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    locations: Mapped[list[Location]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    # Custom presets owned by this user (owner_user_id NOT NULL).
    presets: Mapped[list[Preset]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
    )
