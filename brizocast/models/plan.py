"""``plans`` table — the monetization plan associated one-to-one with a user."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base

if TYPE_CHECKING:
    from brizocast.models.payment import PaymentRecord
    from brizocast.models.user import User


class PlanTier(StrEnum):
    """The monetization tier of a plan (Req 20.2)."""

    FREE = "Free"
    PAID = "Paid"


class PlanStatus(StrEnum):
    """The lifecycle state of a plan (Req 20.2)."""

    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELED = "canceled"


class Plan(Base):
    """A user's membership plan.

    One plan per user (``user_id`` is unique → one-to-one). Created with
    ``tier=Free``/``status=active`` on user creation (Req 20.1-20.4). ``expiry_at``
    is nullable (Free plans have no expiry).
    """

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    tier: Mapped[PlanTier] = mapped_column(
        Enum(
            PlanTier,
            native_enum=False,
            length=16,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=PlanTier.FREE,
        nullable=False,
    )
    status: Mapped[PlanStatus] = mapped_column(
        Enum(
            PlanStatus,
            native_enum=False,
            length=16,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=PlanStatus.ACTIVE,
        nullable=False,
    )
    start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expiry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="plan")
    payments: Mapped[list[PaymentRecord]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
    )
