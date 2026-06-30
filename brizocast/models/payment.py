"""``payment_records`` table — reserved billing records (unpopulated in MVP).

The table structure exists from day one (Req 16.8, 20.5) but is never written
while ``MONETIZATION_ENABLED`` is disabled (Req 20.6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from brizocast.models.plan import Plan


class PaymentRecord(CreatedAtMixin, Base):
    """A billing transaction associated with a user's plan.

    Reserved for future payment integration; not populated in the MVP.
    """

    __tablename__ = "payment_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_txn_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    plan: Mapped[Plan] = relationship(back_populates="payments")
