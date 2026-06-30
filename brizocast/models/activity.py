"""``activities`` table — the supported outdoor sports (Surf in the MVP)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brizocast.models.base import Base

if TYPE_CHECKING:
    from brizocast.models.subscription import Subscription


class Activity(Base):
    """A supported activity, identified by a unique ``key`` (e.g. ``"surf"``).

    ``available_in_mvp`` marks whether the activity is selectable in the MVP;
    only Surf is available initially (Req 1.2, 1.3, 17.1).
    """

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    available_in_mvp: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="activity"
    )
