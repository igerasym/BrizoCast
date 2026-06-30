"""``admin_commands`` table — queued admin actions drained by the bot.

The panel enqueues commands (e.g. run-forecast-check, broadcast) and the bot
process drains them oldest-first with idempotency and per-command isolation.
This decouples the panel process from the bot process (Req 8.1, 9.1, 12.x).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from brizocast.models.base import Base, utcnow


class AdminCommandStatus(StrEnum):
    """Lifecycle state of a queued admin command."""

    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class AdminCommand(Base):
    """A queued admin action awaiting processing by the bot's drain loop.

    ``status`` is indexed so the drain query can efficiently select pending
    commands; ``processed_at`` is set once the command reaches a terminal
    state.
    """

    __tablename__ = "admin_commands"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    status: Mapped[AdminCommandStatus] = mapped_column(
        Enum(
            AdminCommandStatus,
            native_enum=False,
            length=16,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=AdminCommandStatus.PENDING,
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
