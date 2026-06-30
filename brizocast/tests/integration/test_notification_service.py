"""Integration checks for NotificationService record persistence (Req 9.2, 10.2).

Exercises ``NotificationService`` against a real temporary SQLite database to
confirm:

* :meth:`record_sent` persists a ``NotificationSent`` whose fields equal the
  dispatched score/window (subscription, spot, surf score, window key/start/end)
  plus the send timestamp (supports Property 7);
* :meth:`latest_for_window` returns the most recent record for the
  ``(subscription, spot, window)`` dedup identity;
* :meth:`records_since` returns a subscription's records at/after a cutoff for
  digest assembly.

Foreign-key enforcement is off in the MVP (see
``brizocast.database.session``), so notification rows can be written without
seeding parent ``subscriptions``/``surf_spots`` rows.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.core.domain.forecast import ForecastWindow
from brizocast.core.domain.scoring import (
    ScoreBreakdown,
    ScoreCategory,
    ScoreResult,
)
from brizocast.core.domain.scoring_types import FactorContribution
from brizocast.database.session import create_engine, create_session_factory
from brizocast.models import Base
from brizocast.notifications.window import window_key
from brizocast.services.notification_service import NotificationService

SessionFactory = async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    """Build a session factory bound to a fresh temporary SQLite database."""
    db_path = tmp_path / "brizocast_test.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def _factor(value: float, weight: float) -> FactorContribution:
    return FactorContribution(value=value, weight=weight)


def _score_result(score: int, window: ForecastWindow) -> ScoreResult:
    """Build a valid ScoreResult with a complete breakdown for ``score``."""
    breakdown = ScoreBreakdown(
        wave_height=_factor(1.0, 0.30),
        swell_period=_factor(1.0, 0.25),
        wind_speed=_factor(1.0, 0.20),
        wind_direction=_factor(1.0, 0.15),
        swell_direction=_factor(1.0, 0.10),
        total_weighted=score / 100.0,
    )
    return ScoreResult(
        score=score,
        category=ScoreCategory.from_score(score),
        breakdown=breakdown,
        forecast_window=window,
    )


@pytest.mark.asyncio
async def test_record_sent_persists_dispatched_score_and_window(
    session_factory: SessionFactory,
) -> None:
    service = NotificationService(session_factory)
    window = ForecastWindow(
        start=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
        end=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
    )
    sent_at = datetime(2025, 6, 1, 5, 30, tzinfo=UTC)
    result = _score_result(82, window)

    stored = await service.record_sent(
        subscription_id=7,
        spot_key="es-mundaka",
        score_result=result,
        sent_at=sent_at,
    )

    # Persisted record faithfully reflects the dispatched alert (Property 7).
    assert stored.id is not None
    assert stored.subscription_id == 7
    assert stored.spot_key == "es-mundaka"
    assert stored.surf_score == 82
    assert stored.forecast_window_key == window_key(window)
    assert stored.forecast_window_start.replace(tzinfo=None) == window.start.replace(
        tzinfo=None
    )
    assert stored.forecast_window_end.replace(tzinfo=None) == window.end.replace(
        tzinfo=None
    )
    assert stored.sent_at.replace(tzinfo=None) == sent_at.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_record_sent_defaults_timestamp_to_now(
    session_factory: SessionFactory,
) -> None:
    service = NotificationService(session_factory)
    window = ForecastWindow(
        start=datetime(2025, 6, 2, 6, 0, tzinfo=UTC),
        end=datetime(2025, 6, 2, 9, 0, tzinfo=UTC),
    )
    before = datetime.now(UTC).replace(tzinfo=None)

    stored = await service.record_sent(1, "spot", _score_result(70, window))

    after = datetime.now(UTC).replace(tzinfo=None)
    assert before <= stored.sent_at.replace(tzinfo=None) <= after


@pytest.mark.asyncio
async def test_latest_for_window_returns_most_recent(
    session_factory: SessionFactory,
) -> None:
    service = NotificationService(session_factory)
    window = ForecastWindow(
        start=datetime(2025, 6, 3, 6, 0, tzinfo=UTC),
        end=datetime(2025, 6, 3, 9, 0, tzinfo=UTC),
    )
    key = window_key(window)

    await service.record_sent(
        5, "spot", _score_result(60, window),
        sent_at=datetime(2025, 6, 3, 5, 0, tzinfo=UTC),
    )
    newest = await service.record_sent(
        5, "spot", _score_result(75, window),
        sent_at=datetime(2025, 6, 3, 5, 30, tzinfo=UTC),
    )

    latest = await service.latest_for_window(5, "spot", key)
    assert latest is not None
    assert latest.id == newest.id
    assert latest.surf_score == 75


@pytest.mark.asyncio
async def test_latest_for_window_none_when_no_record(
    session_factory: SessionFactory,
) -> None:
    service = NotificationService(session_factory)
    assert await service.latest_for_window(99, "nope", "2025-01-01T00:00Z/3h") is None


@pytest.mark.asyncio
async def test_records_since_filters_by_cutoff(
    session_factory: SessionFactory,
) -> None:
    service = NotificationService(session_factory)
    window = ForecastWindow(
        start=datetime(2025, 6, 4, 6, 0, tzinfo=UTC),
        end=datetime(2025, 6, 4, 9, 0, tzinfo=UTC),
    )

    await service.record_sent(
        3, "spot", _score_result(70, window),
        sent_at=datetime(2025, 6, 1, 12, 0, tzinfo=UTC),
    )
    await service.record_sent(
        3, "spot", _score_result(80, window),
        sent_at=datetime(2025, 6, 5, 12, 0, tzinfo=UTC),
    )

    since = datetime(2025, 6, 4, 0, 0, tzinfo=UTC)
    recent = await service.records_since(3, since)

    assert [r.surf_score for r in recent] == [80]
