"""``custom_conditions`` table — per-subscription condition overrides."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base

if TYPE_CHECKING:
    from brizocast.models.subscription import Subscription


class CustomCondition(Base):
    """User-defined condition overrides for a subscription (Req 4.5, 4.6).

    One-to-one optional with a subscription (``subscription_id`` unique). When
    present, these conditions override any preset during scoring (Req 4.7).
    Holds the ``daylight_only`` flag (Req 8.8) and an optional tide preference.
    """

    __tablename__ = "custom_conditions"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    min_wave_m: Mapped[float] = mapped_column(Float, nullable=False)
    max_wave_m: Mapped[float] = mapped_column(Float, nullable=False)
    min_period_s: Mapped[float] = mapped_column(Float, nullable=False)
    max_wind_kmh: Mapped[float] = mapped_column(Float, nullable=False)
    acceptable_wind_dir: Mapped[str | None] = mapped_column(String(16), nullable=True)
    acceptable_swell_dir: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tide_preference: Mapped[str | None] = mapped_column(String(32), nullable=True)
    daylight_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Additional user-defined alert thresholds
    min_alert_score: Mapped[int | None] = mapped_column(nullable=True)
    min_energy_kw: Mapped[float | None] = mapped_column(Float, nullable=True)

    subscription: Mapped[Subscription] = relationship(
        back_populates="custom_conditions"
    )
