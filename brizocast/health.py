"""Lightweight container health check for BrizoCast.

Runnable as ``python -m brizocast.health`` (used by the Docker Compose
``healthcheck``). It performs a fast liveness/readiness probe and exits ``0``
when the service is healthy or a non-zero code otherwise:

* **Config readiness** — the :class:`~brizocast.config.settings.Settings` model
  loads and validates (so a missing/invalid required value is reported as
  unhealthy).
* **Database reachability** — for SQLite URLs, the database directory exists and
  (if the file is already present) a read-only connection succeeds.

The module is intentionally dependency-light. Everything beyond the standard
library is imported defensively so the probe still runs before the rest of the
application (engine/bootstrap, ``bot.app``) exists. Non-SQLite or in-memory
databases fall back to treating a successful config load as sufficient liveness.

Requirements covered: 15.1, 15.2.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from brizocast.config.settings import Settings

logger = logging.getLogger("brizocast.health")

EXIT_OK = 0
EXIT_UNHEALTHY = 1


def _load_settings() -> Settings | None:
    """Return validated settings, or ``None`` if config cannot be loaded."""

    try:
        from brizocast.config.settings import load_settings
    except Exception:  # noqa: BLE001 - any import failure means "not ready"
        logger.exception("health: unable to import the settings loader")
        return None

    try:
        return load_settings()
    except Exception:  # noqa: BLE001 - validation/load failure means "not ready"
        logger.exception("health: configuration failed to load")
        return None


def _sqlite_path(database_url: str) -> str | None:
    """Return the filesystem path for a SQLite URL, else ``None``.

    ``None`` means the URL is not a file-backed SQLite database (e.g. another
    backend, or an in-memory SQLite database) and therefore needs no file check.
    """

    try:
        from sqlalchemy.engine import make_url
    except Exception:  # noqa: BLE001 - fall back to a manual parse
        return _sqlite_path_fallback(database_url)

    try:
        url = make_url(database_url)
    except Exception:  # noqa: BLE001 - unpar, treat as no file check
        return None

    if not url.drivername.startswith("sqlite"):
        return None
    # ``database`` is None/empty for in-memory SQLite (":memory:" maps to None).
    return url.database or None


def _sqlite_path_fallback(database_url: str) -> str | None:
    """Best-effort SQLite path parse without SQLAlchemy available."""

    if not database_url.startswith("sqlite"):
        return None
    _, _, rest = database_url.partition("://")
    # SQLAlchemy uses three slashes for relative and four for absolute paths;
    # stripping the authority's leading slash yields the on-disk path.
    path = rest[1:] if rest.startswith("/") else rest
    if not path or path == ":memory:":
        return None
    return path


def _check_database(database_url: str) -> bool:
    """Verify a file-backed SQLite database is reachable.

    Returns ``True`` for non-SQLite/in-memory URLs (nothing to check on disk).
    """

    db_path = _sqlite_path(database_url)
    if db_path is None:
        return True

    path = Path(db_path)
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.exists():
        logger.error("health: database directory does not exist: %s", parent)
        return False

    # Only probe the file if it already exists; the bootstrap step creates it on
    # first run, and we must not fabricate it from the health check.
    if path.exists():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                conn.execute("SELECT 1")
            finally:
                conn.close()
        except sqlite3.Error:
            logger.exception("health: database file is not reachable: %s", path)
            return False

    return True


def run_health_check() -> int:
    """Run all checks and return an exit code (``0`` healthy, non-zero otherwise)."""

    settings = _load_settings()
    if settings is None:
        return EXIT_UNHEALTHY

    if not _check_database(settings.DATABASE_URL):
        return EXIT_UNHEALTHY

    logger.info("health: ok")
    return EXIT_OK


def main() -> int:
    """Configure minimal logging and execute the health check."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run_health_check()


if __name__ == "__main__":
    sys.exit(main())
