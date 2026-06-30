"""Alembic migration environment for BrizoCast.

Wires Alembic to the application's ORM metadata (``Base.metadata``) and the
configured ``DATABASE_URL`` (read from Settings, not hard-coded in
``alembic.ini``). Supports both offline (``--sql``) and online migrations; the
online path drives the async engine synchronously via ``run_sync``.

Note: the runtime bootstrap in ``brizocast.database.bootstrap`` is the primary
create/migrate path for the MVP. These migrations are scaffolding for future,
reviewable schema changes.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import Connection

from brizocast.config.settings import load_settings
from brizocast.database.session import create_engine
from brizocast.models import Base

# Alembic Config object, providing access to values within alembic.ini.
config = context.config

# Target metadata for 'autogenerate' support — the full ORM schema.
target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the database URL from Settings, falling back to alembic.ini."""

    try:
        return load_settings().DATABASE_URL
    except Exception:  # noqa: BLE001 - fall back to the ini URL if config is absent.
        return config.get_main_option("sqlalchemy.url", "")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live connection)."""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    """Configure the context against ``connection`` and run migrations."""

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the async engine."""

    engine = create_engine(_database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
