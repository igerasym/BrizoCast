"""Integration checks for FeedbackService persistence (Req 12.4, 12.5).

Exercises ``FeedbackService`` against a real temporary SQLite database to
confirm a 👍/👎 rating is persisted with its subscription, spot, and score, and
that recorded feedback is retained and listable (supports Property 27).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.database.session import (
    create_engine,
    create_session_factory,
    session_scope,
)
from brizocast.models import Base
from brizocast.models.feedback import Feedback, FeedbackRating
from brizocast.services.feedback_service import FeedbackService

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


@pytest.mark.asyncio
async def test_record_feedback_persists_row(
    session_factory: SessionFactory,
) -> None:
    service = FeedbackService(session_factory)

    feedback = await service.record_feedback(
        subscription_id=11,
        spot_key="es/mundaka",
        surf_score=88,
        rating=FeedbackRating.UP,
    )

    assert feedback.id is not None

    async with session_scope(session_factory) as session:
        stored = (
            await session.execute(select(Feedback).where(Feedback.id == feedback.id))
        ).scalar_one()
        assert stored.subscription_id == 11
        assert stored.spot_key == "es/mundaka"
        assert stored.surf_score == 88
        assert stored.rating is FeedbackRating.UP


@pytest.mark.asyncio
async def test_record_feedback_accepts_string_rating(
    session_factory: SessionFactory,
) -> None:
    service = FeedbackService(session_factory)

    feedback = await service.record_feedback(
        subscription_id=3,
        spot_key="pt/ericeira",
        surf_score=51,
        rating="down",
    )

    assert feedback.rating is FeedbackRating.DOWN


@pytest.mark.asyncio
async def test_invalid_string_rating_is_rejected(
    session_factory: SessionFactory,
) -> None:
    service = FeedbackService(session_factory)

    with pytest.raises(ValueError):
        await service.record_feedback(
            subscription_id=1,
            spot_key="x/y",
            surf_score=70,
            rating="sideways",
        )

    # Nothing was persisted for the rejected rating.
    async with session_scope(session_factory) as session:
        count = (
            await session.execute(select(func.count()).select_from(Feedback))
        ).scalar_one()
        assert int(count) == 0


@pytest.mark.asyncio
async def test_list_for_subscription_returns_all_recorded(
    session_factory: SessionFactory,
) -> None:
    service = FeedbackService(session_factory)

    await service.record_feedback(9, "es/mundaka", 90, FeedbackRating.UP)
    await service.record_feedback(9, "es/mundaka", 72, FeedbackRating.DOWN)
    await service.record_feedback(10, "fr/hossegor", 80, FeedbackRating.UP)

    for_nine = await service.list_for_subscription(9)
    assert len(for_nine) == 2
    assert {fb.surf_score for fb in for_nine} == {90, 72}

    for_ten = await service.list_for_subscription(10)
    assert len(for_ten) == 1
    assert for_ten[0].spot_key == "fr/hossegor"
