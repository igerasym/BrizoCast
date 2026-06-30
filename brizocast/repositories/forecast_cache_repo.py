"""SQLAlchemy :class:`ForecastCacheRepository` implementation (Req 7.1-7.5).

``SqlAlchemyForecastCacheRepository`` persists per-spot forecast payloads in the
``forecast_cache`` table, keyed by ``spot_key`` and shared across every
subscription that references the same spot (Req 7.1, 7.5). Each entry stores the
domain :class:`~brizocast.core.domain.forecast.Forecast` serialised to JSON
together with ``fetched_at`` and ``expires_at`` timestamps; the
:class:`~brizocast.services.forecast_service.ForecastService` sets
``expires_at = fetched_at + TTL`` and treats an entry as expired once the
current time reaches ``expires_at`` (Req 7.4).

Self-contained session management
----------------------------------
This repository deliberately does **not** depend on a shared repository base
class. It accepts an injected ``async_sessionmaker[AsyncSession]`` and opens its
own short-lived unit-of-work via
:func:`~brizocast.database.session.session_scope` for every operation, so it can
be composed and tested in isolation.

It conforms structurally to the
:class:`~brizocast.core.ports.repositories.ForecastCacheRepository` port, so the
service layer depends only on the abstraction (Req 16.3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.core.domain.forecast import Forecast
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.database.session import session_scope
from brizocast.models.forecast_cache import ForecastCache

__all__ = ["SqlAlchemyForecastCacheRepository"]


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    SQLite has no native timezone storage, so ``DateTime(timezone=True)`` columns
    round-trip as *naive* datetimes even though every value written by the
    application is aware UTC. Reattaching UTC on read keeps the port's contract
    honest (what was stored is what is returned) and, crucially, lets callers
    compare cached timestamps against an aware ``datetime.now(UTC)`` without a
    naive/aware ``TypeError``.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SqlAlchemyForecastCacheRepository:
    """Persists per-spot cached forecasts via an async SQLAlchemy session.

    A single row is maintained per ``spot_key``: :meth:`put` upserts in place,
    so a spot's cache entry is replaced rather than accumulating duplicates.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the repository.

        Args:
            session_factory: Async session maker used to open a unit of work
                for each operation.
            logger: Optional bound logger; one is created when omitted.
        """
        self._session_factory = session_factory
        self._log = logger or get_logger(__name__)

    async def get(self, spot_key: str) -> ForecastCache | None:
        """Return the cached entry for ``spot_key``, or ``None`` if absent.

        When more than one row somehow exists for a key, the most recently
        fetched entry is returned.
        """
        async with session_scope(self._session_factory) as session:
            stmt = (
                select(ForecastCache)
                .where(ForecastCache.spot_key == spot_key)
                .order_by(ForecastCache.fetched_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            entry = result.scalar_one_or_none()
            if entry is not None:
                # Reattach UTC dropped by SQLite so callers get aware datetimes.
                entry.fetched_at = _as_utc(entry.fetched_at)
                entry.expires_at = _as_utc(entry.expires_at)
            return entry

    async def put(
        self,
        spot_key: str,
        forecast: Forecast,
        fetched_at: datetime,
        expires_at: datetime,
    ) -> None:
        """Upsert the cached ``forecast`` for ``spot_key`` (Req 7.3).

        The domain forecast is serialised to JSON via ``model_dump(mode="json")``
        and stored alongside the fetch/expiry timestamps. An existing entry for
        the key is updated in place; any stray duplicate rows are removed so a
        single current entry remains per spot.
        """
        payload = forecast.model_dump(mode="json")
        async with session_scope(self._session_factory) as session:
            stmt = select(ForecastCache).where(ForecastCache.spot_key == spot_key)
            existing = list((await session.execute(stmt)).scalars().all())
            if existing:
                current = existing[0]
                current.forecast_payload = payload
                current.fetched_at = fetched_at
                current.expires_at = expires_at
                for stale in existing[1:]:
                    await session.delete(stale)
            else:
                session.add(
                    ForecastCache(
                        spot_key=spot_key,
                        forecast_payload=payload,
                        fetched_at=fetched_at,
                        expires_at=expires_at,
                    )
                )

    async def delete_expired(self, now: datetime) -> int:
        """Delete entries whose ``expires_at`` is at or before ``now``.

        Returns the number of rows removed (Req 7.4).
        """
        async with session_scope(self._session_factory) as session:
            stmt = delete(ForecastCache).where(ForecastCache.expires_at <= now)
            result = await session.execute(stmt)
            # DELETE yields a CursorResult exposing the affected row count.
            removed = cast("CursorResult[Any]", result).rowcount
            if removed:
                self._log.debug("evicted %d expired forecast cache entr(y/ies)", removed)
            return removed

    async def clear_all(self) -> int:
        """Delete every cached forecast entry, returning the number removed.

        Backs the admin panel's "clear forecast cache" action (Req 7.4): the
        next forecast request for any spot then refetches from the (possibly
        newly-selected) provider, regardless of each entry's expiry.
        """
        async with session_scope(self._session_factory) as session:
            result = await session.execute(delete(ForecastCache))
            # DELETE yields a CursorResult exposing the affected row count.
            removed = cast("CursorResult[Any]", result).rowcount
            if removed:
                self._log.info("cleared %d forecast cache entr(y/ies)", removed)
            return removed
