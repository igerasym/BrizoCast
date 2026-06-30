"""``config_overrides`` table — live, DB-backed setting overrides.

Stores JSON-encoded override values keyed by setting name (e.g.
``MONETIZATION_ENABLED``). Read by :class:`OverrideAwareSettings` so the admin
panel and bot share a single source of truth for overridable settings without
a redeploy (Req 12.1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from brizocast.models.base import Base, utcnow


class ConfigOverride(Base):
    """A single overridable setting persisted as a JSON value.

    ``key`` is the setting name (its own primary key); ``value`` holds the
    JSON-encoded override (bool, str, float, list, or dict). ``updated_at`` is
    refreshed on every flush so the most recent change is observable.
    """

    __tablename__ = "config_overrides"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
