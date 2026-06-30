"""``presets`` table — region defaults, user customs, and AI-generated presets.

A single table holds all preset provenances with an identical parameter shape,
so an AI-generated preset is interchangeable with a static default everywhere
(Req 16.10, 19.5). ``owner_user_id IS NULL`` ⇒ a default/region preset;
``ai_generated`` distinguishes provenance only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base

if TYPE_CHECKING:
    from brizocast.models.subscription import Subscription
    from brizocast.models.user import User


class Preset(Base):
    """A named, reusable set of surf condition parameters (Req 4.2).

    The surf parameter shape (min/max wave, min period, max wind, preferred
    wind and swell directions) is shared by static, custom, and AI-generated
    presets (Req 16.10, 19.4, 19.5).
    """

    __tablename__ = "presets"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL ⇒ default/region preset; NOT NULL ⇒ user-owned custom preset.
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Surf preset parameter shape (Req 4.2).
    min_wave_m: Mapped[float] = mapped_column(Float, nullable=False)
    max_wave_m: Mapped[float] = mapped_column(Float, nullable=False)
    min_period_s: Mapped[float] = mapped_column(Float, nullable=False)
    max_wind_kmh: Mapped[float] = mapped_column(Float, nullable=False)
    preferred_wind_dir: Mapped[str | None] = mapped_column(String(16), nullable=True)
    preferred_swell_dir: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Minimum score (0-100) to fire an alert for subscriptions using this preset.
    # AI-generated for regional defaults; user can override via custom_conditions.
    min_alert_score: Mapped[int | None] = mapped_column(nullable=True)

    owner: Mapped[User | None] = relationship(back_populates="presets")
    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="preset"
    )
