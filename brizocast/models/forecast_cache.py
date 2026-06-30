"""``forecast_cache`` table — per-spot, TTL'd forecast payloads."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base

if TYPE_CHECKING:
    from brizocast.models.surf_spot import SurfSpot


class ForecastCache(Base):
    """A cached forecast keyed by ``spot_key`` and shared across subscriptions.

    ``expires_at`` is set to ``fetched_at + TTL`` by the cache repository; a
    row is treated as expired once the current time passes ``expires_at``
    (Req 7.1-7.5).
    """

    __tablename__ = "forecast_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    spot_key: Mapped[str] = mapped_column(
        ForeignKey("surf_spots.spot_key"),
        index=True,
        nullable=False,
    )
    forecast_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    surf_spot: Mapped[SurfSpot] = relationship(
        back_populates="forecast_cache_entries"
    )
