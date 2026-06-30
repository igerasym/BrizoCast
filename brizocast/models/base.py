"""Declarative base and reusable column mixins for the ORM layer.

This module defines the single :class:`Base` (a SQLAlchemy 2.0
``DeclarativeBase`` subclass) that all ORM models inherit from, so that they
share one ``MetaData`` registry. It also provides small, typed timestamp
mixins used across the schema.

The whole module targets ``mypy --strict`` and uses the SQLAlchemy 2.0 typed
ORM style (``Mapped[...]`` annotations + ``mapped_column(...)``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``.

    Used as the default factory for timestamp columns so that persisted
    values are always tz-aware and comparable across the application.
    """

    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Shared declarative base for every BrizoCast ORM model.

    All models inherit from this class so they register against a single
    ``MetaData`` instance. Importing :mod:`brizocast.models` registers every
    table on ``Base.metadata``.
    """


class CreatedAtMixin:
    """Mixin adding a non-null, tz-aware ``created_at`` column.

    Applied to entities whose ERD defines a creation timestamp (users, plans,
    payment records, locations, subscriptions, notifications, feedback).
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )


class TimestampMixin(CreatedAtMixin):
    """Mixin adding ``created_at`` and an auto-maintained ``updated_at``.

    Provided for entities that should track their last modification time.
    ``updated_at`` is refreshed on every flush via ``onupdate``. The current
    ERD does not mandate an ``updated_at`` column on any table, so this mixin
    is available for mutable entities that warrant change tracking.
    """

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
