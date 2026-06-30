"""``locations`` table — a named geographic point owned by a user."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from brizocast.models.subscription import Subscription
    from brizocast.models.user import User


class Location(CreatedAtMixin, Base):
    """A geographic point owned by a user.

    ``is_favorite`` distinguishes a saved Favorite_Location from a transient
    one (Req 2.7, 2.8). May be referenced by many subscriptions.
    """

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    city: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="locations")
    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="location"
    )
