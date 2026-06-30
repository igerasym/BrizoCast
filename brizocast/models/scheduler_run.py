"""``scheduler_runs`` table — cross-process visibility of the last run.

A single-row table (``id=1``) recording the last successful forecast-check
run. The bot's in-memory scheduler state is process-local, so the panel (a
separate process) reads this row to render scheduler health (Req 11.2, 11.3).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from brizocast.models.base import Base


class SchedulerRun(Base):
    """Single-row record (``id=1``) of the last successful scheduler run.

    ``last_success_at`` is ``None`` until the first successful run completes,
    representing the "never run" state for the stats page.
    """

    __tablename__ = "scheduler_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
