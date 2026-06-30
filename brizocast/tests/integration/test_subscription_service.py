"""Integration tests for :class:`SubscriptionService` (Req 3.*, 16.6).

These exercise the service end-to-end against a real, file-backed SQLite
database bootstrapped via :func:`brizocast.database.bootstrap.bootstrap_database`,
using the service's own ``session_scope`` unit-of-work. They cover:

* creation persists and binds one user/activity/location (Req 3.1, 3.4, 16.6);
* the radius defaults to 30 km when not provided (Req 3.2);
* radius boundaries 1 and 200 are accepted while 0 and 201 are rejected
  (Req 3.9, 3.10);
* a missing location is rejected (Req 3.8);
* ``list_for_user`` returns all of a user's subscriptions (Req 3.3);
* ``remove`` deletes exactly one subscription, leaving the rest (Req 3.6);
* ``edit_radius`` validates the range and persists the change (Req 3.7).

The named Hypothesis properties for radius validation (Property 17, task 4.8)
and subscription set operations (Property 20, task 4.9) are separate tasks; this
module provides example-based confidence the service wires up correctly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.config.settings import (
    NOTIFICATION_MODE_EVENING_DIGEST,
    NOTIFICATION_MODE_IMMEDIATE,
    NOTIFICATION_MODE_MORNING_DIGEST,
    NOTIFICATION_MODE_WEEKLY_BEST_DAY,
)
from brizocast.core.errors import DomainValidationError, NotFoundError
from brizocast.database.bootstrap import bootstrap_database
from brizocast.database.session import (
    create_engine,
    create_session_factory,
    session_scope,
)
from brizocast.models.activity import Activity
from brizocast.models.location import Location
from brizocast.models.subscription import DEFAULT_SEARCH_RADIUS_KM
from brizocast.models.user import User
from brizocast.repositories.location_repo import SqlAlchemyLocationRepository
from brizocast.repositories.subscription_repo import SqlAlchemySubscriptionRepository
from brizocast.repositories.user_repo import SqlAlchemyUserRepository
from brizocast.services.subscription_service import (
    MAX_SEARCH_RADIUS_KM,
    MIN_SEARCH_RADIUS_KM,
    SubscriptionService,
    SubscriptionSummary,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def session_factory(
    tmp_path: object,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory backed by a fresh temp-file SQLite database."""
    db_path = f"{tmp_path}/subscriptions.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await bootstrap_database(engine)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def _seed_user_activity_location(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    telegram_user_id: int,
) -> tuple[int, int, int]:
    """Seed a user, the Surf activity, and a location; return their ids."""
    async with session_scope(session_factory) as session:
        users = SqlAlchemyUserRepository(session)
        locations = SqlAlchemyLocationRepository(session)
        user = await users.add(User(telegram_user_id=telegram_user_id))
        activity = Activity(key="surf", display_name="🏄 Surf", available_in_mvp=True)
        session.add(activity)
        await session.flush()
        location = await locations.add(Location(user_id=user.id, lat=38.7, lon=-9.4))
        return user.id, activity.id, location.id


async def test_create_persists_and_binds_one_each(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Create persists a subscription bound to one user/activity/location."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=101
    )
    service = SubscriptionService(session_factory)

    created = await service.create(
        user_id, activity_id, location_id, search_radius_km=55.0
    )

    assert created.id is not None
    assert created.user_id == user_id
    assert created.activity_id == activity_id
    assert created.location_id == location_id
    assert created.search_radius_km == pytest.approx(55.0)

    # Verify it actually persisted by reading back in a fresh session.
    async with session_scope(session_factory) as session:
        repo = SqlAlchemySubscriptionRepository(session)
        fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.user_id == user_id
    assert fetched.activity_id == activity_id
    assert fetched.location_id == location_id


async def test_create_defaults_radius_to_20(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Omitting the radius defaults it to 20 km (Req 3.2)."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=102
    )
    service = SubscriptionService(session_factory)

    created = await service.create(user_id, activity_id, location_id)

    assert created.search_radius_km == pytest.approx(DEFAULT_SEARCH_RADIUS_KM)
    assert created.search_radius_km == pytest.approx(20.0)


@pytest.mark.parametrize("radius", [MIN_SEARCH_RADIUS_KM, MAX_SEARCH_RADIUS_KM])
async def test_create_accepts_radius_boundaries(
    session_factory: async_sessionmaker[AsyncSession],
    radius: float,
) -> None:
    """Radii at the inclusive boundaries 1 and 200 are accepted (Req 3.9)."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=int(200 + radius)
    )
    service = SubscriptionService(session_factory)

    created = await service.create(
        user_id, activity_id, location_id, search_radius_km=radius
    )

    assert created.search_radius_km == pytest.approx(radius)


@pytest.mark.parametrize("radius", [0.0, 201.0])
async def test_create_rejects_out_of_range_radius(
    session_factory: async_sessionmaker[AsyncSession],
    radius: float,
) -> None:
    """Radii outside [1, 200] are rejected (Req 3.9, 3.10)."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=int(400 + radius)
    )
    service = SubscriptionService(session_factory)

    with pytest.raises(DomainValidationError):
        await service.create(
            user_id, activity_id, location_id, search_radius_km=radius
        )

    # Nothing was persisted on a rejected create.
    async with session_scope(session_factory) as session:
        repo = SqlAlchemySubscriptionRepository(session)
        assert await repo.count_for_user(user_id) == 0


async def test_create_rejects_missing_location(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Creating without a location is rejected (Req 3.8)."""
    user_id, activity_id, _ = await _seed_user_activity_location(
        session_factory, telegram_user_id=103
    )
    service = SubscriptionService(session_factory)

    with pytest.raises(DomainValidationError):
        await service.create(user_id, activity_id, None)

    async with session_scope(session_factory) as session:
        repo = SqlAlchemySubscriptionRepository(session)
        assert await repo.count_for_user(user_id) == 0


async def test_list_for_user_returns_all(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """list_for_user returns every subscription the user owns (Req 3.3)."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=104
    )
    service = SubscriptionService(session_factory)

    a = await service.create(user_id, activity_id, location_id, search_radius_km=10.0)
    b = await service.create(user_id, activity_id, location_id, search_radius_km=20.0)
    c = await service.create(user_id, activity_id, location_id, search_radius_km=30.0)

    listed = await service.list_for_user(user_id)

    assert {s.id for s in listed} == {a.id, b.id, c.id}
    assert len(listed) == 3


async def test_remove_deletes_exactly_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """remove deletes exactly the selected subscription, leaving the rest (Req 3.6)."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=105
    )
    service = SubscriptionService(session_factory)

    a = await service.create(user_id, activity_id, location_id, search_radius_km=10.0)
    b = await service.create(user_id, activity_id, location_id, search_radius_km=20.0)
    c = await service.create(user_id, activity_id, location_id, search_radius_km=30.0)

    await service.remove(b.id)

    remaining = await service.list_for_user(user_id)
    assert {s.id for s in remaining} == {a.id, c.id}
    assert len(remaining) == 2


async def test_remove_unknown_raises_not_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Removing a non-existent subscription raises NotFoundError."""
    service = SubscriptionService(session_factory)
    with pytest.raises(NotFoundError):
        await service.remove(999_999)


async def test_edit_radius_persists_valid_value(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """edit_radius validates and persists the new radius (Req 3.7)."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=106
    )
    service = SubscriptionService(session_factory)
    created = await service.create(
        user_id, activity_id, location_id, search_radius_km=30.0
    )

    updated = await service.edit_radius(created.id, 75.0)
    assert updated.search_radius_km == pytest.approx(75.0)

    async with session_scope(session_factory) as session:
        repo = SqlAlchemySubscriptionRepository(session)
        fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.search_radius_km == pytest.approx(75.0)


async def test_edit_radius_rejects_out_of_range(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """edit_radius rejects an out-of-range radius without persisting (Req 3.9, 3.10)."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=107
    )
    service = SubscriptionService(session_factory)
    created = await service.create(
        user_id, activity_id, location_id, search_radius_km=30.0
    )

    with pytest.raises(DomainValidationError):
        await service.edit_radius(created.id, 250.0)

    async with session_scope(session_factory) as session:
        repo = SqlAlchemySubscriptionRepository(session)
        fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.search_radius_km == pytest.approx(30.0)


async def test_on_before_create_hook_is_invoked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The monetization extension point is called with the owning user id."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=108
    )
    seen: list[int] = []

    async def guard(uid: int) -> None:
        seen.append(uid)

    service = SubscriptionService(session_factory, on_before_create=guard)
    await service.create(user_id, activity_id, location_id)

    assert seen == [user_id]


async def _seed_user_activity_rich_location(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    telegram_user_id: int,
) -> tuple[int, int, int]:
    """Seed a user, the Surf activity, and a labelled, geocoded location.

    Returns the user, activity, and location ids. The location carries a label,
    city, and country so the summary's label/place resolution can be asserted.
    """
    async with session_scope(session_factory) as session:
        users = SqlAlchemyUserRepository(session)
        locations = SqlAlchemyLocationRepository(session)
        user = await users.add(User(telegram_user_id=telegram_user_id))
        activity = Activity(key="surf", display_name="🏄 Surf", available_in_mvp=True)
        session.add(activity)
        await session.flush()
        location = await locations.add(
            Location(
                user_id=user.id,
                lat=38.7,
                lon=-9.4,
                label="Home break",
                city="Lisbon",
                country="Portugal",
            )
        )
        return user.id, activity.id, location.id


async def test_summarize_for_user_yields_one_entry_per_subscription(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """N subscriptions yield N summaries, each carrying all Req 3.5 fields."""
    user_id, activity_id, location_id = await _seed_user_activity_rich_location(
        session_factory, telegram_user_id=120
    )
    service = SubscriptionService(session_factory)

    created = [
        await service.create(
            user_id,
            activity_id,
            location_id,
            search_radius_km=radius,
            notification_mode=mode,
        )
        for radius, mode in (
            (10.0, NOTIFICATION_MODE_IMMEDIATE),
            (20.0, NOTIFICATION_MODE_MORNING_DIGEST),
            (200.0, NOTIFICATION_MODE_EVENING_DIGEST),
        )
    ]

    summaries = await service.summarize_for_user(user_id)

    # Exactly one summary per subscription (Property 19 completeness).
    assert len(summaries) == len(created)
    assert [s.subscription_id for s in summaries] == [c.id for c in created]

    # Every summary carries activity, location, radius, and mode (Req 3.5).
    for summary, sub in zip(summaries, created, strict=True):
        assert isinstance(summary, SubscriptionSummary)
        assert summary.activity_key == "surf"
        assert summary.activity_display_name == "🏄 Surf"
        assert summary.location_label == "Home break"
        assert summary.location_place == "Lisbon, Portugal"
        assert summary.search_radius_km == pytest.approx(sub.search_radius_km)
        assert summary.notification_mode == sub.notification_mode


async def test_summarize_for_user_is_empty_without_subscriptions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A user who owns no subscriptions gets an empty summary list."""
    user_id, _activity_id, _location_id = await _seed_user_activity_rich_location(
        session_factory, telegram_user_id=121
    )
    service = SubscriptionService(session_factory)

    assert await service.summarize_for_user(user_id) == []


async def test_summarize_for_user_falls_back_to_coordinates_for_bare_location(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A location with no label/city falls back to coordinates (Req 3.5)."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=122
    )
    service = SubscriptionService(session_factory)
    await service.create(user_id, activity_id, location_id, search_radius_km=42.0)

    (summary,) = await service.summarize_for_user(user_id)

    assert summary.location_label == "38.7000, -9.4000"
    assert summary.location_place == "38.7000, -9.4000"
    assert summary.search_radius_km == pytest.approx(42.0)


# --------------------------------------------------------------------------- #
# Notification-preference mutators (task 7.6): set_notification_mode,
# set_quiet_hours, set_muted, snooze (Req 10.2, 11.1, 11.3, 11.4).
# --------------------------------------------------------------------------- #


async def test_set_notification_mode_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """set_notification_mode persists the new mode (Req 10.2)."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=130
    )
    service = SubscriptionService(session_factory)
    created = await service.create(user_id, activity_id, location_id)

    updated = await service.set_notification_mode(
        created.id, NOTIFICATION_MODE_WEEKLY_BEST_DAY
    )
    assert updated.notification_mode == NOTIFICATION_MODE_WEEKLY_BEST_DAY

    async with session_scope(session_factory) as session:
        repo = SqlAlchemySubscriptionRepository(session)
        fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.notification_mode == NOTIFICATION_MODE_WEEKLY_BEST_DAY


async def test_set_notification_mode_rejects_unknown(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unrecognised mode key is rejected (Req 10.2)."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=131
    )
    service = SubscriptionService(session_factory)
    created = await service.create(user_id, activity_id, location_id)

    with pytest.raises(DomainValidationError):
        await service.set_notification_mode(created.id, "nope")


async def test_set_notification_mode_unknown_id_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Editing a non-existent subscription raises NotFoundError."""
    service = SubscriptionService(session_factory)
    with pytest.raises(NotFoundError):
        await service.set_notification_mode(999_999, NOTIFICATION_MODE_IMMEDIATE)


async def test_set_quiet_hours_persists_and_clears(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """set_quiet_hours persists a window and clears it with both None (Req 11.1)."""
    from datetime import time

    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=132
    )
    service = SubscriptionService(session_factory)
    created = await service.create(user_id, activity_id, location_id)

    start, end = time(22, 0), time(7, 0)
    updated = await service.set_quiet_hours(created.id, start, end)
    assert updated.quiet_hours_start == start
    assert updated.quiet_hours_end == end

    cleared = await service.set_quiet_hours(created.id, None, None)
    assert cleared.quiet_hours_start is None
    assert cleared.quiet_hours_end is None


async def test_set_quiet_hours_rejects_half_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Supplying only one bound is rejected (Req 11.1)."""
    from datetime import time

    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=133
    )
    service = SubscriptionService(session_factory)
    created = await service.create(user_id, activity_id, location_id)

    with pytest.raises(DomainValidationError):
        await service.set_quiet_hours(created.id, time(22, 0), None)


async def test_set_muted_toggles(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """set_muted persists both mute and unmute (Req 11.3)."""
    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=134
    )
    service = SubscriptionService(session_factory)
    created = await service.create(user_id, activity_id, location_id)

    muted = await service.set_muted(created.id, True)
    assert muted.muted is True

    async with session_scope(session_factory) as session:
        repo = SqlAlchemySubscriptionRepository(session)
        fetched = await repo.get(created.id)
    assert fetched is not None and fetched.muted is True

    unmuted = await service.set_muted(created.id, False)
    assert unmuted.muted is False


async def test_snooze_sets_and_clears(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """snooze persists a deadline and clears it with None (Req 11.4)."""
    from datetime import UTC, datetime, timedelta

    user_id, activity_id, location_id = await _seed_user_activity_location(
        session_factory, telegram_user_id=135
    )
    service = SubscriptionService(session_factory)
    created = await service.create(user_id, activity_id, location_id)

    until = datetime.now(UTC) + timedelta(hours=3)
    snoozed = await service.snooze(created.id, until)
    assert snoozed.snooze_until is not None

    cleared = await service.snooze(created.id, None)
    assert cleared.snooze_until is None


async def test_mutators_unknown_id_raise(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every mutator raises NotFoundError for an unknown subscription id."""
    from datetime import UTC, datetime, time

    service = SubscriptionService(session_factory)
    with pytest.raises(NotFoundError):
        await service.set_quiet_hours(999_999, time(22, 0), time(7, 0))
    with pytest.raises(NotFoundError):
        await service.set_muted(999_999, True)
    with pytest.raises(NotFoundError):
        await service.snooze(999_999, datetime.now(UTC))
