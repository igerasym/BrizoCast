"""Integration tests for :class:`EntitlementService` (Req 21.*).

These exercise the entitlement gate end-to-end against a real, file-backed
SQLite database bootstrapped via
:func:`brizocast.database.bootstrap.bootstrap_database`, covering both
monetization states:

* **Disabled** — every user has unlimited subscriptions (``math.inf``) and all
  notification modes, and ``assert_can_create_subscription`` is a no-op
  (Req 21.2).
* **Enabled** — limits come from ``Settings.PLAN_LIMITS`` for the user's plan
  tier: creation is allowed under the cap and raises ``QuotaExceededError`` at
  or over it (Req 21.3, 21.4); allowed modes equal the tier's configured set
  (Req 21.5).

They also confirm the composition-root wiring: a ``SubscriptionService`` built
with ``on_before_create=entitlement.assert_can_create_subscription`` rejects an
over-quota create while leaving MVP creation untouched when disabled (Req 21.6).

The named Hypothesis property for config-driven gating (Property 29) is a
separate task; this module gives example-based confidence the service and its
wiring behave correctly.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.config.overrides import OverrideAwareSettings
from brizocast.config.settings import (
    NOTIFICATION_MODE_IMMEDIATE,
    NOTIFICATION_MODE_MORNING_DIGEST,
    PLAN_TIER_FREE,
    PLAN_TIER_PAID,
    PlanLimit,
    Settings,
)
from brizocast.core.errors import QuotaExceededError
from brizocast.database.bootstrap import bootstrap_database
from brizocast.database.session import (
    create_engine,
    create_session_factory,
    session_scope,
)
from brizocast.models.activity import Activity
from brizocast.models.location import Location
from brizocast.models.plan import Plan, PlanStatus, PlanTier
from brizocast.models.user import User
from brizocast.notifications.modes import NotificationMode
from brizocast.repositories.location_repo import SqlAlchemyLocationRepository
from brizocast.repositories.user_repo import SqlAlchemyUserRepository
from brizocast.services.entitlement_service import EntitlementService
from brizocast.services.subscription_service import SubscriptionService

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
async def session_factory(
    tmp_path: object,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory backed by a fresh temp-file SQLite database."""
    db_path = f"{tmp_path}/entitlements.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await bootstrap_database(engine)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


class _NoOverrideStore:
    """In-test override store with no persisted overrides.

    Returning ``None`` for every key drives ``OverrideAwareSettings._resolve``
    down its override-absent branch, so the wrapper always resolves to the
    wrapped ``.env`` ``Settings`` defaults — exactly what these tests assert.
    """

    async def get(self, key: str) -> object | None:
        return None


def _settings(
    *, enabled: bool, plan_limits: dict[str, PlanLimit] | None = None
) -> OverrideAwareSettings:
    """Build override-aware settings (no overrides) with the given config.

    Wraps a ``Settings`` instance in :class:`OverrideAwareSettings` over a
    no-op store so the entitlement service's async accessors resolve straight to
    these ``.env`` baseline values.
    """
    kwargs: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "MONETIZATION_ENABLED": enabled,
    }
    if plan_limits is not None:
        kwargs["PLAN_LIMITS"] = plan_limits
    base = Settings(**kwargs)  # type: ignore[arg-type]
    return OverrideAwareSettings(base, _NoOverrideStore())  # type: ignore[arg-type]


async def _seed_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    telegram_user_id: int,
    tier: PlanTier,
) -> tuple[int, int, int]:
    """Seed a user with a plan of ``tier``, the Surf activity, and a location.

    Returns the user, activity, and location ids.
    """
    from datetime import UTC, datetime

    from sqlalchemy import select

    async with session_scope(session_factory) as session:
        users = SqlAlchemyUserRepository(session)
        locations = SqlAlchemyLocationRepository(session)
        user = await users.add(User(telegram_user_id=telegram_user_id))
        session.add(
            Plan(
                user_id=user.id,
                tier=tier,
                status=PlanStatus.ACTIVE,
                start_at=datetime.now(UTC),
            )
        )
        # The Surf activity key is unique; reuse it across users in one DB.
        activity = (
            await session.execute(select(Activity).where(Activity.key == "surf"))
        ).scalar_one_or_none()
        if activity is None:
            activity = Activity(
                key="surf", display_name="🏄 Surf", available_in_mvp=True
            )
            session.add(activity)
        await session.flush()
        location = await locations.add(Location(user_id=user.id, lat=38.7, lon=-9.4))
        return user.id, activity.id, location.id


# --------------------------------------------------------------------------- #
# Monetization disabled — full access (Req 21.2)
# --------------------------------------------------------------------------- #


async def test_disabled_max_subscriptions_is_unlimited(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """While disabled, the subscription cap is unlimited (Req 21.2)."""
    user_id, _activity_id, _location_id = await _seed_user(
        session_factory, telegram_user_id=201, tier=PlanTier.FREE
    )
    service = EntitlementService(session_factory, _settings(enabled=False))

    assert await service.max_subscriptions_for(user_id) == math.inf


async def test_disabled_allows_all_notification_modes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """While disabled, every notification mode is allowed (Req 21.2, 21.5)."""
    user_id, _activity_id, _location_id = await _seed_user(
        session_factory, telegram_user_id=202, tier=PlanTier.FREE
    )
    service = EntitlementService(session_factory, _settings(enabled=False))

    assert await service.allowed_notification_modes(user_id) == set(NotificationMode)


async def test_disabled_assert_is_noop_even_over_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """While disabled, creation is never gated regardless of count (Req 21.2)."""
    user_id, activity_id, location_id = await _seed_user(
        session_factory, telegram_user_id=203, tier=PlanTier.FREE
    )
    entitlement = EntitlementService(session_factory, _settings(enabled=False))
    subscriptions = SubscriptionService(
        session_factory, on_before_create=entitlement.assert_can_create_subscription
    )

    # Free tier defaults to 2; create well beyond it — all must succeed.
    for _ in range(5):
        await subscriptions.create(user_id, activity_id, location_id)

    listed = await subscriptions.list_for_user(user_id)
    assert len(listed) == 5


# --------------------------------------------------------------------------- #
# Monetization enabled — config-driven limits (Req 21.1, 21.3, 21.4, 21.5)
# --------------------------------------------------------------------------- #


async def test_enabled_max_subscriptions_matches_tier_config(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """While enabled, the cap is the tier's configured max (Req 21.1, 21.3)."""
    limits = {
        PLAN_TIER_FREE: PlanLimit(
            max_subscriptions=2, notification_modes={NOTIFICATION_MODE_IMMEDIATE}
        ),
        PLAN_TIER_PAID: PlanLimit(
            max_subscriptions=10,
            notification_modes={
                NOTIFICATION_MODE_IMMEDIATE,
                NOTIFICATION_MODE_MORNING_DIGEST,
            },
        ),
    }
    free_id, _a, _l = await _seed_user(
        session_factory, telegram_user_id=210, tier=PlanTier.FREE
    )
    paid_id, _a2, _l2 = await _seed_user(
        session_factory, telegram_user_id=211, tier=PlanTier.PAID
    )
    service = EntitlementService(
        session_factory, _settings(enabled=True, plan_limits=limits)
    )

    assert await service.max_subscriptions_for(free_id) == 2
    assert await service.max_subscriptions_for(paid_id) == 10


async def test_enabled_allowed_modes_match_tier_config(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """While enabled, allowed modes equal the tier's configured set (Req 21.5)."""
    limits = {
        PLAN_TIER_FREE: PlanLimit(
            max_subscriptions=2,
            notification_modes={
                NOTIFICATION_MODE_IMMEDIATE,
                NOTIFICATION_MODE_MORNING_DIGEST,
            },
        ),
        PLAN_TIER_PAID: PlanLimit(
            max_subscriptions=10, notification_modes=set(NotificationMode)
        ),
    }
    free_id, _a, _l = await _seed_user(
        session_factory, telegram_user_id=212, tier=PlanTier.FREE
    )
    service = EntitlementService(
        session_factory, _settings(enabled=True, plan_limits=limits)
    )

    assert await service.allowed_notification_modes(free_id) == {
        NotificationMode.IMMEDIATE,
        NotificationMode.MORNING_DIGEST,
    }


async def test_enabled_allows_under_limit_then_blocks_at_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Creation succeeds under the cap and raises QuotaExceededError at it (Req 21.4)."""
    limits = {
        PLAN_TIER_FREE: PlanLimit(
            max_subscriptions=2, notification_modes={NOTIFICATION_MODE_IMMEDIATE}
        ),
    }
    user_id, activity_id, location_id = await _seed_user(
        session_factory, telegram_user_id=213, tier=PlanTier.FREE
    )
    entitlement = EntitlementService(
        session_factory, _settings(enabled=True, plan_limits=limits)
    )
    subscriptions = SubscriptionService(
        session_factory, on_before_create=entitlement.assert_can_create_subscription
    )

    # Two creations are allowed (count 0 -> 1, 1 -> 2, both below/at start).
    await subscriptions.create(user_id, activity_id, location_id)
    await subscriptions.create(user_id, activity_id, location_id)

    # The third would exceed the cap of 2 and is rejected, carrying the limit.
    with pytest.raises(QuotaExceededError) as exc_info:
        await subscriptions.create(user_id, activity_id, location_id)
    assert exc_info.value.limit == 2

    # Exactly two persisted — the rejected create added nothing (Req 21.6).
    listed = await subscriptions.list_for_user(user_id)
    assert len(listed) == 2


async def test_enabled_assert_raises_when_count_at_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """assert_can_create_subscription raises when count already meets the cap."""
    limits = {
        PLAN_TIER_FREE: PlanLimit(
            max_subscriptions=1, notification_modes={NOTIFICATION_MODE_IMMEDIATE}
        ),
    }
    user_id, activity_id, location_id = await _seed_user(
        session_factory, telegram_user_id=214, tier=PlanTier.FREE
    )
    entitlement = EntitlementService(
        session_factory, _settings(enabled=True, plan_limits=limits)
    )

    # No subscriptions yet -> under the cap of 1 -> no raise.
    await entitlement.assert_can_create_subscription(user_id)

    # Persist one directly (ungated), reaching the cap.
    ungated = SubscriptionService(session_factory)
    await ungated.create(user_id, activity_id, location_id)

    # Now at the cap of 1 -> the next create must be blocked.
    with pytest.raises(QuotaExceededError):
        await entitlement.assert_can_create_subscription(user_id)


async def test_enabled_defaults_to_free_when_user_has_no_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A user without a plan is treated as Free (Req 21.1)."""
    limits = {
        PLAN_TIER_FREE: PlanLimit(
            max_subscriptions=3, notification_modes={NOTIFICATION_MODE_IMMEDIATE}
        ),
    }
    # Seed a bare user with no Plan row.
    async with session_scope(session_factory) as session:
        users = SqlAlchemyUserRepository(session)
        user = await users.add(User(telegram_user_id=215))
        user_id = user.id

    service = EntitlementService(
        session_factory, _settings(enabled=True, plan_limits=limits)
    )

    assert await service.max_subscriptions_for(user_id) == 3
