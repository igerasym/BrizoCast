"""DB-backed scheduler-run state (Req 11.2, 11.3).

The bot's :class:`~brizocast.services.scheduler_state.InMemorySchedulerState`
holds the last successful forecast-check time in process memory, which is
invisible to the admin panel running as a *separate process*. This module
provides :class:`SqliteSchedulerState`, a drop-in for that holder that persists
the timestamp in the single-row ``scheduler_runs`` table (``id=1``) so the
panel's stats view can read the last successful run cross-process (Req 11.2,
11.3).

Both scheduler-run ports in :mod:`brizocast.services.scheduler_state` are
**async**, so this class performs its SQLAlchemy I/O directly in
:meth:`record_success_async` and :meth:`last_successful_run_async` without any
sync-over-async bridge: the scheduler runner awaits the durable write on the
running event loop, and both the bot's ``/status`` and the admin panel read the
persisted value the same way.

Requirements covered: 11.2, 11.3.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from brizocast.database.session import session_scope
from brizocast.models.scheduler_run import SchedulerRun

if TYPE_CHECKING:
    from brizocast.core.container import SessionFactory

__all__ = ["SqliteSchedulerState"]

# The fixed primary key of the single ``scheduler_runs`` row both processes share.
_SCHEDULER_RUN_ID = 1


class SqliteSchedulerState:
    """Persisted last-successful-run state over the ``scheduler_runs`` row ``id=1``.

    Satisfies both :class:`~brizocast.services.scheduler_state.SchedulerRunReader`
    and :class:`~brizocast.services.scheduler_state.SchedulerRunRecorder` by
    reading and upserting the shared ``scheduler_runs`` row, so a separate
    process (the admin panel) can read the last successful run the bot recorded.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        """Initialise the state holder.

        Args:
            session_factory: The application's ``async_sessionmaker``; each async
                read/write opens one session from it via ``session_scope``.
        """
        self._session_factory = session_factory

    async def last_successful_run_async(self) -> datetime | None:
        """Return the persisted most-recent successful run, or ``None``.

        Reads ``scheduler_runs`` row ``id=1`` from the shared database. ``None``
        means no successful run has been persisted yet (the stats view renders
        this as "never", Req 11.3). This is the accessor both the bot's
        ``/status`` and the admin panel use to observe the last run.
        """
        async with session_scope(self._session_factory) as session:
            row = await session.get(SchedulerRun, _SCHEDULER_RUN_ID)
            return None if row is None else row.last_success_at

    async def record_success_async(self, when: datetime) -> None:
        """Upsert ``when`` into ``scheduler_runs`` row ``id=1``."""
        async with session_scope(self._session_factory) as session:
            row = await session.get(SchedulerRun, _SCHEDULER_RUN_ID)
            if row is None:
                session.add(
                    SchedulerRun(id=_SCHEDULER_RUN_ID, last_success_at=when)
                )
            else:
                row.last_success_at = when
