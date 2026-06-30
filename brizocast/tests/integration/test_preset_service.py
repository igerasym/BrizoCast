"""Integration tests for :class:`PresetService` (Req 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9).

Exercised end-to-end against a real, file-backed SQLite database bootstrapped
via :func:`brizocast.database.bootstrap.bootstrap_database`, using the service's
own ``session_scope`` unit-of-work. Coverage:

* ``list_presets`` returns bundled static defaults plus the user's custom
  presets (Req 4.3);
* ``select_default`` associates a persisted preset with a subscription (Req 4.4)
  and raises for unknown ids;
* ``create_custom_conditions`` persists overrides (Req 4.5, 4.6) and rejects an
  inverted wave band with ``DomainValidationError`` (Req 4.8);
* ``resolve_effective_conditions`` follows the order custom → selected preset →
  region's first default (Req 4.7, 4.9; Property 14).

The named Hypothesis properties (Property 14 effective-conditions resolution,
task 4.11; Property 18 wave-band validation, task 4.12) are separate tasks; this
module provides example-based confidence the service wires up correctly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.activities.surf.conditions import SurfConditions, TidePreference
from brizocast.activities.surf.presets import first_default_for_region
from brizocast.core.errors import DomainValidationError, NotFoundError
from brizocast.database.bootstrap import bootstrap_database
from brizocast.database.session import (
    create_engine,
    create_session_factory,
    session_scope,
)
from brizocast.models.activity import Activity
from brizocast.models.custom_condition import CustomCondition
from brizocast.models.location import Location
from brizocast.models.preset import Preset
from brizocast.models.subscription import Subscription
from brizocast.models.user import User
from brizocast.repositories.subscription_repo import SqlAlchemySubscriptionRepository
from brizocast.services.preset_service import PresetService, PresetSource

pytestmark = pytest.mark.integration


@pytest.fixture
async def session_factory(
    tmp_path: object,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory backed by a fresh temp-file SQLite database."""
    db_path = f"{tmp_path}/presets.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await bootstrap_database(engine)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def _seed_subscription(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    telegram_user_id: int,
) -> tuple[int, int]:
    """Seed a user, activity, location, and subscription; return (user_id, sub_id)."""
    async with session_scope(session_factory) as session:
        user = User(telegram_user_id=telegram_user_id)
        session.add(user)
        activity = Activity(key="surf", display_name="🏄 Surf", available_in_mvp=True)
        session.add(activity)
        await session.flush()
        location = Location(user_id=user.id, lat=39.34, lon=-9.36)
        session.add(location)
        await session.flush()
        sub = Subscription(
            user_id=user.id,
            activity_id=activity.id,
            location_id=location.id,
        )
        session.add(sub)
        await session.flush()
        return user.id, sub.id


async def _add_preset(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    name: str,
    region: str | None,
    owner_user_id: int | None,
    is_default: bool,
) -> int:
    """Insert a preset row and return its id."""
    async with session_scope(session_factory) as session:
        preset = Preset(
            owner_user_id=owner_user_id,
            name=name,
            region=region,
            is_default=is_default,
            min_wave_m=1.0,
            max_wave_m=2.5,
            min_period_s=9.0,
            max_wind_kmh=24.0,
            preferred_wind_dir="E",
            preferred_swell_dir="W",
        )
        session.add(preset)
        await session.flush()
        return preset.id


async def test_list_presets_includes_persisted_defaults_and_user_customs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """list_presets returns persisted defaults plus the user's custom presets (Req 4.3)."""
    user_id, _ = await _seed_subscription(session_factory, telegram_user_id=701)
    await _add_preset(
        session_factory,
        name="My Custom",
        region=None,
        owner_user_id=user_id,
        is_default=False,
    )
    service = PresetService(session_factory)

    options = await service.list_presets(user_id, region="Northern Poland")

    by_source = {opt.source for opt in options}
    # No static presets for "Northern Poland" — only generic fallback.
    assert PresetSource.STATIC_DEFAULT in by_source
    assert PresetSource.USER_CUSTOM in by_source
    # Generic fallback defaults are present.
    assert any(opt.is_default for opt in options)
    # The user's custom preset is present and carries a database id.
    customs = [opt for opt in options if opt.source is PresetSource.USER_CUSTOM]
    assert len(customs) == 1
    assert customs[0].name == "My Custom"
    assert customs[0].preset_id is not None


async def test_select_default_associates_preset_with_subscription(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """select_default sets the subscription's preset_id (Req 4.4)."""
    user_id, sub_id = await _seed_subscription(session_factory, telegram_user_id=702)
    preset_id = await _add_preset(
        session_factory,
        name="Peniche Default",
        region="Peniche",
        owner_user_id=None,
        is_default=True,
    )
    service = PresetService(session_factory)

    updated = await service.select_default(sub_id, preset_id)
    assert updated.preset_id == preset_id

    async with session_scope(session_factory) as session:
        repo = SqlAlchemySubscriptionRepository(session)
        fetched = await repo.get(sub_id)
    assert fetched is not None
    assert fetched.preset_id == preset_id


async def test_select_default_unknown_preset_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Selecting a non-existent preset raises NotFoundError."""
    _, sub_id = await _seed_subscription(session_factory, telegram_user_id=703)
    service = PresetService(session_factory)
    with pytest.raises(NotFoundError):
        await service.select_default(sub_id, 999_999)


async def test_create_custom_conditions_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """create_custom_conditions persists overrides for the subscription (Req 4.5, 4.6)."""
    _, sub_id = await _seed_subscription(session_factory, telegram_user_id=704)
    service = PresetService(session_factory)
    conditions = SurfConditions(
        min_wave_m=1.0,
        max_wave_m=2.0,
        min_period_s=10.0,
        max_wind_kmh=20.0,
        preferred_wind_dir_deg=90.0,  # E
        preferred_swell_dir_deg=270.0,  # W
        tide_preference=TidePreference.MID,
        daylight_only=True,
    )

    created = await service.create_custom_conditions(sub_id, conditions)
    assert created.subscription_id == sub_id

    async with session_scope(session_factory) as session:
        from brizocast.repositories.condition_repo import (
            SqlAlchemyCustomConditionRepository,
        )

        repo = SqlAlchemyCustomConditionRepository(session)
        stored = await repo.get_for_subscription(sub_id)
    assert stored is not None
    assert stored.min_wave_m == pytest.approx(1.0)
    assert stored.max_wave_m == pytest.approx(2.0)
    assert stored.acceptable_wind_dir == "E"
    assert stored.acceptable_swell_dir == "W"
    assert stored.tide_preference == "mid"
    assert stored.daylight_only is True


async def test_create_custom_conditions_updates_existing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-entering custom conditions updates the single override in place (Req 4.7)."""
    _, sub_id = await _seed_subscription(session_factory, telegram_user_id=705)
    service = PresetService(session_factory)
    first = SurfConditions(
        min_wave_m=1.0, max_wave_m=2.0, min_period_s=9.0, max_wind_kmh=20.0
    )
    second = SurfConditions(
        min_wave_m=1.5, max_wave_m=3.0, min_period_s=11.0, max_wind_kmh=28.0
    )

    await service.create_custom_conditions(sub_id, first)
    await service.create_custom_conditions(sub_id, second)

    async with session_scope(session_factory) as session:
        from sqlalchemy import select

        rows = (
            await session.execute(
                select(CustomCondition).where(
                    CustomCondition.subscription_id == sub_id
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].max_wave_m == pytest.approx(3.0)


async def test_create_custom_conditions_rejects_inverted_wave_band(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """min wave > max wave is rejected before persisting (Req 4.8)."""
    _, sub_id = await _seed_subscription(session_factory, telegram_user_id=706)
    service = PresetService(session_factory)

    # SurfConditions itself rejects an inverted band at construction.
    with pytest.raises((DomainValidationError, ValueError)):
        bad = SurfConditions(
            min_wave_m=3.0, max_wave_m=1.0, min_period_s=9.0, max_wind_kmh=20.0
        )
        await service.create_custom_conditions(sub_id, bad)

    # Nothing persisted.
    async with session_scope(session_factory) as session:
        from brizocast.repositories.condition_repo import (
            SqlAlchemyCustomConditionRepository,
        )

        repo = SqlAlchemyCustomConditionRepository(session)
        assert await repo.get_for_subscription(sub_id) is None


async def test_resolve_prefers_custom_then_preset_then_region_default(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Effective conditions follow custom → preset → region default (Req 4.7, 4.9)."""
    _, sub_id = await _seed_subscription(session_factory, telegram_user_id=707)
    service = PresetService(session_factory)

    # 3) No custom, no preset → region's first default for "Peniche".
    async with session_scope(session_factory) as session:
        repo = SqlAlchemySubscriptionRepository(session)
        sub = await repo.get(sub_id)
        assert sub is not None
    fallback = await service.resolve_effective_conditions(sub, region="Peniche")
    expected_default = first_default_for_region("Peniche").to_conditions()
    assert fallback.min_wave_m == pytest.approx(expected_default.min_wave_m)
    assert fallback.max_wave_m == pytest.approx(expected_default.max_wave_m)

    # 2) Selected preset wins over the region default.
    preset_id = await _add_preset(
        session_factory,
        name="Chosen",
        region="Peniche",
        owner_user_id=None,
        is_default=True,
    )
    await service.select_default(sub_id, preset_id)
    async with session_scope(session_factory) as session:
        repo = SqlAlchemySubscriptionRepository(session)
        sub = await repo.get(sub_id)
        assert sub is not None
    from_preset = await service.resolve_effective_conditions(sub, region="Peniche")
    assert from_preset.min_wave_m == pytest.approx(1.0)
    assert from_preset.max_wave_m == pytest.approx(2.5)
    assert from_preset.max_wind_kmh == pytest.approx(24.0)

    # 1) Custom conditions win over the selected preset.
    custom = SurfConditions(
        min_wave_m=0.5, max_wave_m=1.2, min_period_s=7.0, max_wind_kmh=15.0
    )
    await service.create_custom_conditions(sub_id, custom)
    async with session_scope(session_factory) as session:
        repo = SqlAlchemySubscriptionRepository(session)
        sub = await repo.get(sub_id)
        assert sub is not None
    effective = await service.resolve_effective_conditions(sub, region="Peniche")
    assert effective.min_wave_m == pytest.approx(0.5)
    assert effective.max_wave_m == pytest.approx(1.2)
    assert effective.max_wind_kmh == pytest.approx(15.0)
