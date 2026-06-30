"""Integration tests for :class:`SqlAlchemyForecastCacheRepository` (task 3.8).

Exercises the repository against a real (temp-file) SQLite database created via
the schema bootstrap, covering: insert + round-trip get, JSON serialisation of
the domain :class:`Forecast`, in-place upsert (one row per ``spot_key``), a miss
returning ``None``, and expired-entry eviction (Req 7.1, 7.3, 7.4, 7.5).

Foreign-key enforcement is off in the MVP (see ``database/session.py``), so
forecast-cache rows for JSON-sourced ``spot_key`` values persist without a
matching ``surf_spots`` row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.core.domain.forecast import Forecast, ForecastStep
from brizocast.database.bootstrap import bootstrap_database
from brizocast.database.session import create_engine, create_session_factory
from brizocast.repositories.forecast_cache_repo import (
    SqlAlchemyForecastCacheRepository,
)

pytestmark = pytest.mark.integration


def _make_forecast(spot_key: str, *, wave: float = 1.4) -> Forecast:
    return Forecast(
        spot_id=spot_key,
        steps=[
            ForecastStep(
                timestamp=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
                wave_height_m=wave,
                swell_period_s=12.0,
                swell_direction_deg=290.0,
                wind_speed_kmh=6.0,
                wind_direction_deg=80.0,
            )
        ],
    )


@pytest_asyncio.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide a bootstrapped temp-file SQLite session factory."""
    db_path = tmp_path / "brizocast-test.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        await bootstrap_database(engine)
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def test_put_then_get_round_trips_forecast(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyForecastCacheRepository(session_factory)
    fetched_at = datetime(2025, 6, 1, 5, 0, tzinfo=UTC)
    expires_at = fetched_at + timedelta(minutes=180)
    forecast = _make_forecast("pt/peniche-supertubos", wave=1.8)

    await repo.put("pt/peniche-supertubos", forecast, fetched_at, expires_at)
    entry = await repo.get("pt/peniche-supertubos")

    assert entry is not None
    assert entry.spot_key == "pt/peniche-supertubos"
    assert Forecast.model_validate(entry.forecast_payload) == forecast


async def test_get_missing_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyForecastCacheRepository(session_factory)
    assert await repo.get("does/not-exist") is None


async def test_put_upserts_single_row_per_spot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyForecastCacheRepository(session_factory)
    first_at = datetime(2025, 6, 1, 5, 0, tzinfo=UTC)
    second_at = datetime(2025, 6, 1, 8, 0, tzinfo=UTC)
    ttl = timedelta(minutes=180)

    await repo.put("es/mundaka", _make_forecast("es/mundaka", wave=1.0), first_at, first_at + ttl)
    await repo.put("es/mundaka", _make_forecast("es/mundaka", wave=2.5), second_at, second_at + ttl)

    entry = await repo.get("es/mundaka")
    assert entry is not None
    assert entry.fetched_at == second_at
    refreshed = Forecast.model_validate(entry.forecast_payload)
    assert refreshed.steps[0].wave_height_m == 2.5


async def test_delete_expired_removes_only_stale_entries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyForecastCacheRepository(session_factory)
    now = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)

    # One already-expired entry, one still fresh.
    await repo.put(
        "old/spot",
        _make_forecast("old/spot"),
        now - timedelta(hours=6),
        now - timedelta(hours=3),
    )
    await repo.put(
        "fresh/spot",
        _make_forecast("fresh/spot"),
        now - timedelta(minutes=30),
        now + timedelta(hours=2),
    )

    removed = await repo.delete_expired(now)

    assert removed == 1
    assert await repo.get("old/spot") is None
    assert await repo.get("fresh/spot") is not None
