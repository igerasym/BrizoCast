"""Integration checks for UserService Free-plan provisioning (Req 1.7, 20.*).

Exercises ``UserService`` against a real temporary SQLite database to confirm:

* a first call creates exactly one user and exactly one Free, active plan,
  atomically (``start_at`` set, ``expiry_at`` NULL);
* repeated calls are idempotent — still one user and one plan, returned
  unchanged (supports Property 21);
* helper mutators (``mark_onboarded``, ``set_selected_activity``) persist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
from brizocast.models import Base, Plan, PlanStatus, PlanTier, User
from brizocast.services.user_service import UserService

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


async def _count(session_factory: SessionFactory, model: type[Base]) -> int:
    async with session_scope(session_factory) as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_first_call_creates_user_and_free_active_plan(
    session_factory: SessionFactory,
) -> None:
    fixed_now = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    service = UserService(session_factory, now=lambda: fixed_now)

    user = await service.get_or_create_user(123456789, username="surfer")

    assert user.telegram_user_id == 123456789
    assert user.username == "surfer"
    assert await _count(session_factory, User) == 1
    assert await _count(session_factory, Plan) == 1

    async with session_scope(session_factory) as session:
        plan = (
            await session.execute(select(Plan).where(Plan.user_id == user.id))
        ).scalar_one()
        assert plan.tier is PlanTier.FREE
        assert plan.status is PlanStatus.ACTIVE
        # SQLite returns naive datetimes; compare the wall-clock value.
        assert plan.start_at.replace(tzinfo=None) == fixed_now.replace(tzinfo=None)
        assert plan.expiry_at is None


@pytest.mark.asyncio
async def test_repeated_calls_are_idempotent(
    session_factory: SessionFactory,
) -> None:
    service = UserService(session_factory)

    first = await service.get_or_create_user(42, username="a")
    second = await service.get_or_create_user(42, username="ignored-on-second")
    third = await service.get_or_create_user(42)

    assert first.id == second.id == third.id
    # username is only set at creation; later calls leave the user unchanged.
    assert second.username == "a"
    assert await _count(session_factory, User) == 1
    assert await _count(session_factory, Plan) == 1


@pytest.mark.asyncio
async def test_distinct_ids_get_distinct_users_and_plans(
    session_factory: SessionFactory,
) -> None:
    service = UserService(session_factory)

    await service.get_or_create_user(1)
    await service.get_or_create_user(2)
    await service.get_or_create_user(1)

    assert await _count(session_factory, User) == 2
    assert await _count(session_factory, Plan) == 2


@pytest.mark.asyncio
async def test_helpers_persist_onboarding_and_activity(
    session_factory: SessionFactory,
) -> None:
    service = UserService(session_factory)
    await service.get_or_create_user(7)

    await service.set_selected_activity(7, "surf")
    onboarded = await service.mark_onboarded(7)

    assert onboarded.onboarded is True
    assert onboarded.selected_activity_key == "surf"

    reread = await service.get_by_telegram_id(7)
    assert reread is not None
    assert reread.onboarded is True
    assert reread.selected_activity_key == "surf"


@pytest.mark.asyncio
async def test_helpers_reject_unknown_user(
    session_factory: SessionFactory,
) -> None:
    service = UserService(session_factory)
    with pytest.raises(ValueError):
        await service.mark_onboarded(999)
    with pytest.raises(ValueError):
        await service.set_selected_activity(999, "surf")
