"""Integration round-trip tests for the SQLAlchemy repositories (Req 16.3).

These exercise the repositories against a real, file-backed SQLite database
bootstrapped via :func:`brizocast.database.bootstrap.bootstrap_database`, using
the caller-owned ``session_scope`` unit-of-work documented in
:mod:`brizocast.repositories.base`. They cover the core add/get round-trips for
user, plan, location, and subscription, plus a multi-repository atomic write.

A full Hypothesis property test for the persistence round-trip (Property 15)
lives in task 4.2; this module provides example-based confidence that the
repositories wire up correctly end-to-end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.database.bootstrap import bootstrap_database
from brizocast.database.session import (
    create_engine,
    create_session_factory,
    session_scope,
)
from brizocast.models.activity import Activity
from brizocast.models.location import Location
from brizocast.models.plan import Plan, PlanStatus, PlanTier
from brizocast.models.subscription import Subscription
from brizocast.models.user import User
from brizocast.repositories.location_repo import SqlAlchemyLocationRepository
from brizocast.repositories.plan_repo import SqlAlchemyPlanRepository
from brizocast.repositories.subscription_repo import SqlAlchemySubscriptionRepository
from brizocast.repositories.user_repo import SqlAlchemyUserRepository

pytestmark = pytest.mark.integration


@pytest.fixture
async def session_factory(tmp_path: object) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory backed by a fresh temp-file SQLite database."""
    db_path = f"{tmp_path}/roundtrip.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await bootstrap_database(engine)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def _seed_surf_activity(session: AsyncSession) -> Activity:
    """Insert and return the Surf activity row (FK target for subscriptions)."""
    activity = Activity(key="surf", display_name="🏄 Surf", available_in_mvp=True)
    session.add(activity)
    await session.flush()
    return activity


async def test_user_add_get_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A persisted user reads back by id and by Telegram id with equal fields."""
    async with session_scope(session_factory) as session:
        repo = SqlAlchemyUserRepository(session)
        stored = await repo.add(
            User(telegram_user_id=4242, username="alice", onboarded=True)
        )
        user_id = stored.id

    async with session_scope(session_factory) as session:
        repo = SqlAlchemyUserRepository(session)
        by_id = await repo.get(user_id)
        by_tg = await repo.get_by_telegram_id(4242)

    assert by_id is not None
    assert by_tg is not None
    assert by_id.id == by_tg.id == user_id
    assert by_id.telegram_user_id == 4242
    assert by_id.username == "alice"
    assert by_id.onboarded is True


async def test_plan_add_get_for_user_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A user's plan reads back via get_for_user with equal fields."""
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    async with session_scope(session_factory) as session:
        users = SqlAlchemyUserRepository(session)
        plans = SqlAlchemyPlanRepository(session)
        user = await users.add(User(telegram_user_id=1001))
        await plans.add(
            Plan(
                user_id=user.id,
                tier=PlanTier.FREE,
                status=PlanStatus.ACTIVE,
                start_at=start,
            )
        )
        user_id = user.id

    async with session_scope(session_factory) as session:
        plans = SqlAlchemyPlanRepository(session)
        plan = await plans.get_for_user(user_id)

    assert plan is not None
    assert plan.user_id == user_id
    assert plan.tier is PlanTier.FREE
    assert plan.status is PlanStatus.ACTIVE
    assert plan.expiry_at is None


async def test_location_add_list_favorites_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Favorites filter returns only saved locations for the user."""
    async with session_scope(session_factory) as session:
        users = SqlAlchemyUserRepository(session)
        locations = SqlAlchemyLocationRepository(session)
        user = await users.add(User(telegram_user_id=2002))
        await locations.add(
            Location(
                user_id=user.id,
                label="Home break",
                lat=43.4,
                lon=-1.5,
                city="Biarritz",
                country="FR",
                is_favorite=True,
            )
        )
        await locations.add(
            Location(user_id=user.id, lat=10.0, lon=20.0, is_favorite=False)
        )
        user_id = user.id

    async with session_scope(session_factory) as session:
        locations = SqlAlchemyLocationRepository(session)
        all_for_user = await locations.list_for_user(user_id)
        favorites = await locations.list_favorites(user_id)

    assert len(all_for_user) == 2
    assert len(favorites) == 1
    fav = favorites[0]
    assert fav.label == "Home break"
    assert fav.lat == pytest.approx(43.4)
    assert fav.lon == pytest.approx(-1.5)
    assert fav.city == "Biarritz"
    assert fav.is_favorite is True


async def test_subscription_add_get_and_count_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A subscription persists across user/activity/location and reads back."""
    async with session_scope(session_factory) as session:
        users = SqlAlchemyUserRepository(session)
        locations = SqlAlchemyLocationRepository(session)
        subs = SqlAlchemySubscriptionRepository(session)
        activity = await _seed_surf_activity(session)
        user = await users.add(User(telegram_user_id=3003))
        location = await locations.add(
            Location(user_id=user.id, lat=38.7, lon=-9.4)
        )
        stored = await subs.add(
            Subscription(
                user_id=user.id,
                activity_id=activity.id,
                location_id=location.id,
                search_radius_km=42.0,
                notification_mode="morning_digest",
            )
        )
        sub_id = stored.id
        user_id = user.id

    async with session_scope(session_factory) as session:
        subs = SqlAlchemySubscriptionRepository(session)
        fetched = await subs.get(sub_id)
        active = await subs.list_all_active()
        count = await subs.count_for_user(user_id)

    assert fetched is not None
    assert fetched.id == sub_id
    assert fetched.user_id == user_id
    assert fetched.search_radius_km == pytest.approx(42.0)
    assert fetched.notification_mode == "morning_digest"
    assert fetched.active is True
    assert [s.id for s in active] == [sub_id]
    assert count == 1


async def test_subscription_delete_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deleting a subscription removes it from subsequent reads."""
    async with session_scope(session_factory) as session:
        users = SqlAlchemyUserRepository(session)
        locations = SqlAlchemyLocationRepository(session)
        subs = SqlAlchemySubscriptionRepository(session)
        activity = await _seed_surf_activity(session)
        user = await users.add(User(telegram_user_id=5005))
        location = await locations.add(
            Location(user_id=user.id, lat=1.0, lon=2.0)
        )
        stored = await subs.add(
            Subscription(
                user_id=user.id,
                activity_id=activity.id,
                location_id=location.id,
            )
        )
        sub_id = stored.id
        user_id = user.id

    async with session_scope(session_factory) as session:
        subs = SqlAlchemySubscriptionRepository(session)
        await subs.delete(sub_id)

    async with session_scope(session_factory) as session:
        subs = SqlAlchemySubscriptionRepository(session)
        assert await subs.get(sub_id) is None
        assert await subs.count_for_user(user_id) == 0
