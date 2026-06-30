"""``surf_spots`` table — discoverable surf locations.

In the MVP, spots are loaded from ``storage/spots/surf_spots.json`` by the
``JsonSpotRepository``; this table is provided for a future DB-backed
``SpotRepository`` implementation (Req 5.1, 5.2, 5.6). ``spot_key`` is the
stable identifier referenced by forecast cache, notification, and feedback
rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base

if TYPE_CHECKING:
    from brizocast.models.feedback import Feedback
    from brizocast.models.forecast_cache import ForecastCache
    from brizocast.models.notification import NotificationSent


class SurfSpot(Base):
    """A surf location with a unique ``spot_key`` and geographic coordinates."""

    __tablename__ = "surf_spots"

    id: Mapped[int] = mapped_column(primary_key=True)
    spot_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    country: Mapped[str | None] = mapped_column(String(255), nullable=True)
    region: Mapped[str | None] = mapped_column(String(255), nullable=True)

    forecast_cache_entries: Mapped[list[ForecastCache]] = relationship(
        back_populates="surf_spot"
    )
    notifications: Mapped[list[NotificationSent]] = relationship(
        back_populates="surf_spot"
    )
    feedback: Mapped[list[Feedback]] = relationship(back_populates="surf_spot")
