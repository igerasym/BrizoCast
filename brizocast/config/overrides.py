"""Live, DB-backed configuration overrides and an override-aware settings facade.

The admin panel and the bot run as separate processes with no IPC channel; they
rendezvous on the shared SQLite database. Overridable settings (the monetization
flag, plan-limit map, and forecast provider) are persisted in the
``config_overrides`` table and read at runtime through
:class:`OverrideAwareSettings`, which resolves *override-first, else ``.env``
default* and **re-reads the store on every access** (no process-lifetime
caching). This is what lets a change made in the panel apply to the running bot
without a restart (Req 6.4, 7.3, 12.2).

:class:`ConfigOverrideStore` is a thin repository over the ``config_overrides``
table; it stores JSON-encoded values keyed by setting name and refreshes
``updated_at`` on every upsert. It follows the same unit-of-work boundary as the
rest of the service layer, opening one session per call via
:func:`brizocast.database.session.session_scope`.

Requirements covered: 6.4, 7.3, 12.2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from brizocast.config.settings import PlanLimit, Settings
from brizocast.database.session import session_scope
from brizocast.models.base import utcnow
from brizocast.models.config_override import ConfigOverride

if TYPE_CHECKING:
    from brizocast.core.container import SessionFactory

__all__ = [
    "OVERRIDE_KEYS",
    "VALID_LOG_LEVELS",
    "ConfigOverrideStore",
    "OverrideAwareSettings",
]


# The set of setting names the panel may override at runtime. Every other field
# proxies straight through to the validated ``.env`` ``Settings`` object.
OVERRIDE_KEYS = frozenset(
    {"MONETIZATION_ENABLED", "PLAN_LIMITS", "FORECAST_PROVIDER", "LOG_LEVEL"}
)

# Valid logging levels the panel may select.
VALID_LOG_LEVELS = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


def _coerce_bool(value: object) -> bool:
    """Coerce a JSON-decoded override (or ``.env`` default) into a ``bool``.

    JSON booleans round-trip as ``bool`` already; strings/integers are accepted
    defensively so a value persisted as ``"true"`` or ``1`` still resolves
    correctly.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class ConfigOverrideStore:
    """Thin repository over the ``config_overrides`` table (JSON values).

    Each method opens a single session from the injected ``async_sessionmaker``
    via :func:`session_scope`, which commits on success and rolls back on error.
    Values are stored and returned as JSON-decoded Python objects.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        """Initialise the store.

        Args:
            session_factory: The application's ``async_sessionmaker``. Each call
                opens one session from it via ``session_scope``.
        """
        self._session_factory = session_factory

    async def get(self, key: str) -> object | None:
        """Return the decoded JSON override for ``key`` or ``None`` if absent."""
        async with session_scope(self._session_factory) as session:
            row = await session.get(ConfigOverride, key)
            return None if row is None else row.value

    async def set(self, key: str, value: object) -> None:
        """Upsert ``key`` to ``value``, refreshing ``updated_at`` to now."""
        async with session_scope(self._session_factory) as session:
            existing = await session.get(ConfigOverride, key)
            if existing is None:
                session.add(
                    ConfigOverride(key=key, value=value, updated_at=utcnow())
                )
            else:
                existing.value = value
                existing.updated_at = utcnow()

    async def all(self) -> dict[str, object]:
        """Return every persisted override as a ``{key: decoded value}`` map."""
        async with session_scope(self._session_factory) as session:
            result = await session.execute(select(ConfigOverride))
            return {row.key: row.value for row in result.scalars().all()}


class OverrideAwareSettings:
    """Settings facade resolving persisted overrides first, else ``.env``.

    Exposes async accessors for the three overridable settings and proxies every
    other attribute straight to the wrapped :class:`Settings` via
    ``__getattr__`` — so the wrapper is a drop-in for call sites that do not need
    an override. Override lookups re-read the store on **every** call, with no
    process-lifetime caching, so a write between two reads changes the second
    read's result (Req 6.4, 7.3, 12.2).
    """

    def __init__(self, base: Settings, store: ConfigOverrideStore) -> None:
        """Wrap ``base`` ``.env`` settings with the ``config_overrides`` store."""
        self._base = base
        self._store = store

    async def monetization_enabled(self) -> bool:
        """Resolve the monetization flag, override-first else ``.env`` (Req 6.4)."""
        return _coerce_bool(
            await self._resolve("MONETIZATION_ENABLED", self._base.MONETIZATION_ENABLED)
        )

    async def plan_limits(self) -> dict[str, PlanLimit]:
        """Resolve the plan-limit map, reconstructing ``PlanLimit`` from JSON.

        Falls back to the ``.env`` default when no override is persisted or when
        the stored value is not a JSON object (Req 6.4).
        """
        raw = await self._resolve("PLAN_LIMITS", None)
        if not isinstance(raw, dict):
            return self._base.PLAN_LIMITS
        return {str(key): PlanLimit(**value) for key, value in raw.items()}

    async def forecast_provider(self) -> str:
        """Resolve the forecast provider id, override-first else ``.env`` (Req 7.3)."""
        return str(await self._resolve("FORECAST_PROVIDER", self._base.FORECAST_PROVIDER))

    async def log_level(self) -> str:
        """Resolve the log level, override-first else ``.env``.

        Falls back to the ``.env`` ``LOG_LEVEL`` when no override is persisted or
        when the stored value is not a recognised level name.
        """
        raw = await self._resolve("LOG_LEVEL", self._base.LOG_LEVEL)
        level = str(raw).strip().upper()
        return level if level in VALID_LOG_LEVELS else self._base.LOG_LEVEL

    async def _resolve(self, key: str, default: object) -> object:
        """Return the persisted override for ``key`` if present, else ``default``."""
        override = await self._store.get(key)  # Req 12.2 precedence
        return default if override is None else override

    def __getattr__(self, name: str) -> object:
        """Proxy non-overridable attribute access to the wrapped ``Settings``."""
        return getattr(self._base, name)
