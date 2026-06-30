"""Schema bootstrap: create-if-absent and migrate/recreate-on-incompatible.

On startup the application calls :func:`bootstrap_database` with an async
engine. The coroutine guarantees the database ends up at the current
:data:`SCHEMA_VERSION`:

* **Absent schema** (a fresh database with none of our tables) → the full
  schema is created via ``Base.metadata.create_all`` and the schema version is
  stamped (Req 16.4).
* **Compatible schema** (our tables exist and the stored version equals
  :data:`SCHEMA_VERSION`) → no-op, so repeated calls are idempotent.
* **Incompatible schema** (our tables exist but the stored version differs from
  :data:`SCHEMA_VERSION`) → the schema is *recreated* (drop-all + create-all)
  and re-stamped (Req 16.5). For the MVP a recreate-on-incompatible strategy is
  acceptable; Alembic (see :mod:`brizocast.database.migrations`) provides the
  path to true migrations later.

The stored schema version is tracked using SQLite's built-in
``PRAGMA user_version`` — a per-database integer that defaults to ``0`` on a
fresh file and persists across connections. This avoids an extra meta table.

This module imports :data:`brizocast.models.Base`, which registers every table
on ``Base.metadata`` (so ``create_all`` / ``drop_all`` see the whole schema).

Requirements covered: 16.4, 16.5.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from brizocast.core.logging import get_logger
from brizocast.models import Base

__all__ = ["SCHEMA_VERSION", "bootstrap_database"]

logger = get_logger(__name__)

# Bump this whenever the ORM schema changes in a way incompatible with an
# already-deployed database. A mismatch between the value stored in the
# database (``PRAGMA user_version``) and this constant triggers the
# migrate/recreate path in :func:`bootstrap_database` (Req 16.5).
#
# Version history:
#   1 — initial schema.
#   2 — added admin tables (config_overrides, admin_commands, scheduler_runs);
#       deployments at version 1 recreate cleanly via the recreate-on-
#       incompatible path.
SCHEMA_VERSION: Final[int] = 2


def _read_user_version(connection: Connection) -> int:
    """Return SQLite's ``PRAGMA user_version`` for the connected database."""

    result = connection.exec_driver_sql("PRAGMA user_version")
    value = result.scalar()
    return int(value) if value is not None else 0


def _write_user_version(connection: Connection, version: int) -> None:
    """Stamp the database with ``version`` via ``PRAGMA user_version``.

    ``PRAGMA user_version`` does not accept bound parameters, so the (trusted,
    integer) value is formatted directly. ``version`` is always an ``int``
    derived from :data:`SCHEMA_VERSION`, so this is not an injection vector.
    """

    connection.exec_driver_sql(f"PRAGMA user_version = {int(version)}")


def _schema_is_present(connection: Connection) -> bool:
    """Whether any table defined on ``Base.metadata`` already exists."""

    existing = set(inspect(connection).get_table_names())
    declared = set(Base.metadata.tables.keys())
    return bool(existing & declared)


def _create_schema(connection: Connection) -> None:
    """Create all tables and stamp the current schema version."""

    Base.metadata.create_all(connection)
    _write_user_version(connection, SCHEMA_VERSION)


def _recreate_schema(connection: Connection) -> None:
    """Drop all known tables and recreate them at the current schema version."""

    Base.metadata.drop_all(connection)
    Base.metadata.create_all(connection)
    _write_user_version(connection, SCHEMA_VERSION)


def _bootstrap_sync(connection: Connection) -> None:
    """Synchronous bootstrap body, run inside the async connection's greenlet."""

    if not _schema_is_present(connection):
        logger.info(
            "Database schema absent; creating schema at version %d",
            SCHEMA_VERSION,
        )
        _create_schema(connection)
        return

    stored_version = _read_user_version(connection)
    if stored_version == SCHEMA_VERSION:
        logger.debug(
            "Database schema present and compatible (version %d); no action",
            stored_version,
        )
        return

    logger.warning(
        "Database schema version %d is incompatible with current version %d; "
        "recreating schema (MVP recreate-on-incompatible strategy)",
        stored_version,
        SCHEMA_VERSION,
    )
    _recreate_schema(connection)


async def bootstrap_database(engine: AsyncEngine) -> None:
    """Ensure the database is at the current schema version.

    Creates the schema if absent (Req 16.4) and migrates/recreates it when the
    stored schema version is incompatible with :data:`SCHEMA_VERSION`
    (Req 16.5). Safe to call on every startup: when the schema is already
    present and compatible the call is a no-op (idempotent).

    Args:
        engine: An async engine (typically built by
            :func:`brizocast.database.session.create_engine`).
    """

    async with engine.begin() as connection:
        await connection.run_sync(_bootstrap_sync)
