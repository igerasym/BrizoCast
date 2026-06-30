"""Async SQLAlchemy engine, session factory, and connection PRAGMA setup.

This module owns the low-level persistence plumbing for BrizoCast:

* :func:`create_engine` builds a SQLAlchemy 2.x **async** engine from a
  database URL (``sqlite+aiosqlite://`` in the MVP) and installs a ``connect``
  event listener that configures SQLite for concurrent, durable operation
  (WAL journal mode). See the *Foreign-key enforcement* note below for why FK
  enforcement is intentionally left at SQLite's default in the MVP.
* :func:`create_session_factory` builds an ``async_sessionmaker`` with
  ``expire_on_commit=False`` so attributes remain accessible after a commit.
* :func:`session_scope` is a typed async context manager that yields an
  :class:`~sqlalchemy.ext.asyncio.AsyncSession`, commits on success, rolls back
  on error, and always closes the session.

It deliberately does **not** read configuration or wire itself into the
application — composition (building the engine from
:class:`brizocast.config.settings.Settings` and bootstrapping the schema)
happens at the application root (task 11.1). Callers pass an explicit
database URL / engine, which keeps this module trivially testable against a
temporary database.

Foreign-key enforcement decision (MVP)
--------------------------------------
SQLite ships with foreign-key enforcement **disabled** and toggles it per
connection via ``PRAGMA foreign_keys`` — it is all-or-nothing for a given
connection; SQLite cannot enforce some foreign keys while ignoring others.

Three tables carry a ``spot_key`` foreign key to ``surf_spots``
(``forecast_cache``, ``notifications_sent``, ``feedback``). In the MVP, surf
spots are served from the bundled JSON dataset by ``JsonSpotRepository`` and
**no rows are written to the ``surf_spots`` table**. If FK enforcement were
turned on, every forecast-cache / notification / feedback write for a
JSON-sourced ``spot_key`` would be rejected with a foreign-key violation,
breaking core MVP behaviour.

Turning enforcement on while keeping caching working would require dropping the
``spot_key`` foreign keys (and reworking the associated ORM relationships) on
the task 1.4 models — an invasive change to a different task's deliverable.

Therefore the MVP keeps FK enforcement at SQLite's default (**off**): the
``spot_key`` foreign keys remain in the schema as documentation of intent, the
task 1.4 models and their relationships are left untouched, and MVP forecast
caching / notifications / feedback all work. Enforcement can be enabled (set
``PRAGMA foreign_keys=ON`` in :func:`_configure_sqlite_connection`) once a
DB-backed ``SpotRepository`` populates ``surf_spots``.

Requirements covered (with :mod:`brizocast.database.bootstrap`): 16.4, 16.5.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brizocast.core.logging import get_logger

__all__ = [
    "create_engine",
    "create_session_factory",
    "session_scope",
]

logger = get_logger(__name__)

# SQLite database targets that are not real files on disk and therefore need no
# parent-directory creation.
_SQLITE_NON_FILE_TARGETS: Final[frozenset[str | None]] = frozenset({None, "", ":memory:"})


def _configure_sqlite_connection(dbapi_connection: Any, _record: Any) -> None:
    """Apply per-connection SQLite PRAGMAs (runs on every new DBAPI connection).

    Enables WAL journal mode for better read/write concurrency and durability
    (design: "SQLite (WAL mode)"). Foreign-key enforcement is intentionally
    left at SQLite's default of *off* for the MVP — see the module docstring
    for the full rationale.
    """

    cursor = dbapi_connection.cursor()
    try:
        # WAL allows concurrent readers alongside a writer and survives the
        # scheduler's frequent short transactions well.
        cursor.execute("PRAGMA journal_mode=WAL")
        # Reasonable durability without the cost of full FSYNC on every commit.
        cursor.execute("PRAGMA synchronous=NORMAL")
        # NOTE: FK enforcement deliberately NOT enabled in the MVP. To turn it
        # on once ``surf_spots`` is DB-backed, uncomment the next line:
        #   cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """Create the parent directory for a file-backed SQLite database if needed.

    SQLite raises ``unable to open database file`` if the directory containing
    the database file does not exist. For the default URL
    (``sqlite+aiosqlite:///data/brizocast.db``) this creates ``data/`` on first
    use. In-memory and non-file targets are skipped.
    """

    url = make_url(database_url)
    if not url.get_backend_name().startswith("sqlite"):
        return
    database = url.database
    if database in _SQLITE_NON_FILE_TARGETS:
        return
    assert database is not None  # narrowed by the membership check above
    parent = Path(database).expanduser().parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create an async engine and install SQLite connection PRAGMAs.

    Args:
        database_url: A SQLAlchemy async URL, e.g.
            ``sqlite+aiosqlite:///data/brizocast.db``.
        echo: When ``True``, log emitted SQL (useful in tests/debugging).

    Returns:
        A configured :class:`~sqlalchemy.ext.asyncio.AsyncEngine`. For SQLite
        URLs the parent directory of a file-backed database is created if
        absent and a ``connect`` listener applies the WAL PRAGMA on every new
        connection.
    """

    _ensure_sqlite_parent_dir(database_url)
    engine = create_async_engine(database_url, echo=echo, future=True)
    if engine.dialect.name == "sqlite":
        # The listener is attached to the underlying sync engine; it fires
        # synchronously with each new DBAPI connection the async engine opens.
        event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
    logger.debug("Created async engine for dialect %s", engine.dialect.name)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build an ``async_sessionmaker`` bound to ``engine``.

    ``expire_on_commit=False`` keeps ORM attributes loaded after ``commit`` so
    callers can read entity fields without triggering a fresh (and, on an async
    session, awkwardly lazy) database round-trip.
    """

    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a transactional :class:`AsyncSession` from ``session_factory``.

    The session is committed when the ``async with`` block exits normally and
    rolled back if it raises; either way the session is closed. Use this as the
    standard unit-of-work boundary in services and jobs::

        async with session_scope(session_factory) as session:
            ...  # use session
    """

    session = session_factory()
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()
