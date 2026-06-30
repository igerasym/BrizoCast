"""Shared last-scheduler-run state (Req 13.3, 14.4; Req 11.2, 11.3).

This module defines the shared place where the most recent **successful**
scheduler-run time is written and read:

* the scheduler runner (task 8.3) holds a :class:`SchedulerRunRecorder` and
  awaits :meth:`SchedulerRunRecorder.record_success_async` once a forecast-check
  job completes successfully — and only then, leaving the timestamp unchanged
  when a job fails before completing (Req 14.4, 14.5);
* :class:`~brizocast.services.status_service.StatusService` holds a
  :class:`SchedulerRunReader` and awaits it for ``/status`` (Req 13.3),
  returning ``None`` (rendered as "never") until the scheduler has completed at
  least one run.

Both ports are **async** so a persisted implementation (the SQLite-backed
``scheduler_runs`` row used by :class:`~brizocast.services.sqlite_scheduler_state.SqliteSchedulerState`,
Req 11.2, 11.3) can perform real database I/O on the running event loop without
the sync-over-async bridge a synchronous port would force. The
in-memory :class:`InMemorySchedulerState` satisfies the same async ports with
trivial wrappers over a held value, so test fakes and the bot share one shape.

The two narrow ports keep the dependency direction one-way: the reader never
sees the recorder's write API and vice-versa. A single shared instance is wired
at the composition root (task 11.1) so the recorder the scheduler writes to and
the reader ``StatusService`` reads from observe the same state.

Wiring contract for task 8.3
----------------------------
The scheduler runner accepts a :class:`SchedulerRunRecorder` and, on successful
job completion, awaits ``recorder.record_success_async(now)`` with a
timezone-aware UTC ``now``. If that write fails, the job is still treated as
completed and the failure is logged (Req 14.6); the runner guards the call in a
``try``/``except`` for that reason.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = [
    "InMemorySchedulerState",
    "SchedulerRunReader",
    "SchedulerRunRecorder",
]


@runtime_checkable
class SchedulerRunReader(Protocol):
    """Read side of the scheduler-run state (consumed by ``StatusService``)."""

    async def last_successful_run_async(self) -> datetime | None:
        """Return the most recent successful scheduler-run time, or ``None``.

        ``None`` means the scheduler has not completed a run yet; ``/status``
        renders this as "never".
        """
        ...


@runtime_checkable
class SchedulerRunRecorder(Protocol):
    """Write side of the scheduler-run state (used by the scheduler, task 8.3)."""

    async def record_success_async(self, when: datetime) -> None:
        """Record ``when`` as the most recent successful scheduler-run time.

        Called by the runner only after a forecast-check job completes
        successfully (Req 14.4); a failing job must not call this so the prior
        timestamp is left unchanged (Req 14.5).
        """
        ...


class InMemorySchedulerState:
    """In-memory holder of the most recent successful scheduler-run time.

    Satisfies both :class:`SchedulerRunReader` and :class:`SchedulerRunRecorder`
    via trivial async wrappers over a held value, so one shared instance bridges
    the scheduler runner (writes) and ``StatusService`` (reads). State lives only
    in memory: a process restart resets the last-run time to ``None`` ("never")
    until the next successful run. Used by the tests as a fake; the bot wires the
    persisted :class:`~brizocast.services.sqlite_scheduler_state.SqliteSchedulerState`.
    """

    def __init__(self, *, initial: datetime | None = None) -> None:
        """Initialise the holder.

        Args:
            initial: Optional seed value for the last successful run; defaults to
                ``None`` (no run recorded yet).
        """
        self._last_run = initial

    async def last_successful_run_async(self) -> datetime | None:
        """Return the most recent recorded successful run, or ``None``."""
        return self._last_run

    async def record_success_async(self, when: datetime) -> None:
        """Store ``when`` as the most recent successful scheduler-run time."""
        self._last_run = when
