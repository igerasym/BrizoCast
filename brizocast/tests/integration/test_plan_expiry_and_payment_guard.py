"""Integration checks for the plan-expiry check and payment guard (Req 20.5-20.7).

Exercises ``PlanExpiryService`` and ``PaymentRecordingService`` against a real
temporary SQLite database to confirm:

* a Paid plan whose ``expiry_at`` is earlier than the injected ``now`` is
  flipped to ``expired`` by the expiry check, and the check is idempotent
  (Req 20.7);
* an active/non-expired Paid plan (and a Free plan) is left untouched;
* while ``MONETIZATION_ENABLED`` is disabled the payment guard raises and never
  populates ``payment_records`` (Req 20.5, 20.6);
* while enabled the guard persists a payment record (the reserved future path).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.config.settings import Settings
from brizocast.core.errors import MonetizationDisabledError
from brizocast.database.session import (
    create_engine,
    create_session_factory,
    session_scope,
)
from brizocast.models import (
    Base,
    PaymentRecord,
    Plan,
    PlanStatus,
    PlanTier,
    User,
)
from brizocast.services.payment_service import PaymentRecordingService
from brizocast.services.plan_expiry_service import PlanExpiryService

SessionFactory = async_sessionmaker[AsyncSession]

FIXED_NOW = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


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


def _settings(*, monetization_enabled: bool) -> Settings:
    """Build a Settings instance with the monetization flag set explicitly."""
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MONETIZATION_ENABLED=monetization_enabled,
    )


async def _add_user_with_plan(
    session_factory: SessionFactory,
    *,
    tier: PlanTier,
    status: PlanStatus,
    expiry_at: datetime | None,
    telegram_user_id: int,
) -> int:
    """Persist a user and one plan; return the plan id."""
    async with session_scope(session_factory) as session:
        user = User(telegram_user_id=telegram_user_id)
        session.add(user)
        await session.flush()
        plan = Plan(
            user_id=user.id,
            tier=tier,
            status=status,
            start_at=FIXED_NOW - timedelta(days=30),
            expiry_at=expiry_at,
        )
        session.add(plan)
        await session.flush()
        return plan.id


async def _status_of(session_factory: SessionFactory, plan_id: int) -> PlanStatus:
    async with session_scope(session_factory) as session:
        plan = (
            await session.execute(select(Plan).where(Plan.id == plan_id))
        ).scalar_one()
        return plan.status


async def _count(session_factory: SessionFactory, model: type[Base]) -> int:
    async with session_scope(session_factory) as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_paid_plan_past_expiry_flips_to_expired(
    session_factory: SessionFactory,
) -> None:
    plan_id = await _add_user_with_plan(
        session_factory,
        tier=PlanTier.PAID,
        status=PlanStatus.ACTIVE,
        expiry_at=FIXED_NOW - timedelta(seconds=1),
        telegram_user_id=1,
    )
    service = PlanExpiryService(session_factory, now=lambda: FIXED_NOW)

    expired = await service.run()

    assert expired == 1
    assert await _status_of(session_factory, plan_id) is PlanStatus.EXPIRED


@pytest.mark.asyncio
async def test_active_non_expired_paid_plan_is_untouched(
    session_factory: SessionFactory,
) -> None:
    future = await _add_user_with_plan(
        session_factory,
        tier=PlanTier.PAID,
        status=PlanStatus.ACTIVE,
        expiry_at=FIXED_NOW + timedelta(days=1),
        telegram_user_id=2,
    )
    free = await _add_user_with_plan(
        session_factory,
        tier=PlanTier.FREE,
        status=PlanStatus.ACTIVE,
        expiry_at=None,
        telegram_user_id=3,
    )
    service = PlanExpiryService(session_factory, now=lambda: FIXED_NOW)

    expired = await service.run()

    assert expired == 0
    assert await _status_of(session_factory, future) is PlanStatus.ACTIVE
    assert await _status_of(session_factory, free) is PlanStatus.ACTIVE


@pytest.mark.asyncio
async def test_expiry_check_is_idempotent(
    session_factory: SessionFactory,
) -> None:
    plan_id = await _add_user_with_plan(
        session_factory,
        tier=PlanTier.PAID,
        status=PlanStatus.ACTIVE,
        expiry_at=FIXED_NOW - timedelta(hours=1),
        telegram_user_id=4,
    )
    service = PlanExpiryService(session_factory, now=lambda: FIXED_NOW)

    first = await service.run()
    second = await service.run()

    assert first == 1
    assert second == 0
    assert await _status_of(session_factory, plan_id) is PlanStatus.EXPIRED


@pytest.mark.asyncio
async def test_callable_alias_runs_the_check(
    session_factory: SessionFactory,
) -> None:
    plan_id = await _add_user_with_plan(
        session_factory,
        tier=PlanTier.PAID,
        status=PlanStatus.ACTIVE,
        expiry_at=FIXED_NOW - timedelta(minutes=5),
        telegram_user_id=5,
    )
    service = PlanExpiryService(session_factory, now=lambda: FIXED_NOW)

    expired = await service()

    assert expired == 1
    assert await _status_of(session_factory, plan_id) is PlanStatus.EXPIRED


@pytest.mark.asyncio
async def test_payment_guard_blocks_while_monetization_disabled(
    session_factory: SessionFactory,
) -> None:
    plan_id = await _add_user_with_plan(
        session_factory,
        tier=PlanTier.PAID,
        status=PlanStatus.ACTIVE,
        expiry_at=FIXED_NOW + timedelta(days=30),
        telegram_user_id=6,
    )
    service = PaymentRecordingService(
        session_factory, _settings(monetization_enabled=False)
    )

    with pytest.raises(MonetizationDisabledError):
        await service.record_payment(plan_id, amount_cents=999, currency="USD")

    # The reserved table must remain empty (Req 20.6).
    assert await _count(session_factory, PaymentRecord) == 0


@pytest.mark.asyncio
async def test_payment_guard_persists_while_monetization_enabled(
    session_factory: SessionFactory,
) -> None:
    plan_id = await _add_user_with_plan(
        session_factory,
        tier=PlanTier.PAID,
        status=PlanStatus.ACTIVE,
        expiry_at=FIXED_NOW + timedelta(days=30),
        telegram_user_id=7,
    )
    service = PaymentRecordingService(
        session_factory, _settings(monetization_enabled=True)
    )

    record = await service.record_payment(
        plan_id, provider="stripe", amount_cents=1500, currency="USD", status="paid"
    )

    assert record.plan_id == plan_id
    assert await _count(session_factory, PaymentRecord) == 1
