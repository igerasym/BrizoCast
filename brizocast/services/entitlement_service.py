"""Entitlement / monetization gate service (Req 21.*).

``EntitlementService`` is the **single, isolated** place that turns a user's
:term:`Plan` and the configured :term:`Plan_Limits` into concrete entitlements:
how many subscriptions the user may own and which notification modes they may
use. It is consulted only at the subscription-creation boundary — wired into
:class:`~brizocast.services.subscription_service.SubscriptionService` through its
``on_before_create`` hook — so the MVP creation, notification, and scoring logic
stays completely untouched (Req 21.6, design "Monetization gate as a guard").

Everything the service decides is driven by configuration, never hard-coded:

* When :attr:`Settings.MONETIZATION_ENABLED` is ``False`` every user has full
  access — unlimited subscriptions (``math.inf``) and every notification mode —
  and :meth:`assert_can_create_subscription` is a no-op (Req 21.2).
* When enabled, limits come from :attr:`Settings.PLAN_LIMITS` indexed by the
  user's :class:`~brizocast.models.plan.PlanTier` (Req 21.1, 21.3): creation is
  allowed iff the current subscription count is below the tier's
  ``max_subscriptions`` (Req 21.4), and the allowed notification modes equal the
  tier's configured set (Req 21.5).

Because both the flag and the limits are read fresh from :class:`Settings` on
every call, changing configuration changes behaviour with no code edits
(Req 21.7).

Requirements covered: 21.1, 21.2, 21.3, 21.4, 21.5, 21.6, 21.7 (supports
Property 29).
"""

from __future__ import annotations

import math

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.config.overrides import OverrideAwareSettings
from brizocast.config.settings import (
    PLAN_TIER_FREE,
    PLAN_TIER_PAID,
    PlanLimit,
)
from brizocast.core.errors import QuotaExceededError
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.database.session import session_scope
from brizocast.models.plan import PlanTier
from brizocast.models.user import User
from brizocast.notifications.modes import NotificationMode
from brizocast.repositories.plan_repo import SqlAlchemyPlanRepository
from brizocast.repositories.subscription_repo import SqlAlchemySubscriptionRepository

__all__ = ["EntitlementService"]

# Map the ORM :class:`PlanTier` (values "Free"/"Paid") onto the configuration
# keys of :attr:`Settings.PLAN_LIMITS` ("free"/"paid"). Keeping the two key
# spaces explicit here means neither the model nor the config layer has to know
# about the other (Req 21.1, 21.3).
_TIER_TO_LIMIT_KEY: dict[PlanTier, str] = {
    PlanTier.FREE: PLAN_TIER_FREE,
    PlanTier.PAID: PLAN_TIER_PAID,
}


class EntitlementService:
    """Resolve entitlements from a user's plan and gate subscription creation.

    The service owns its unit-of-work boundary: the ``*_for`` /
    :meth:`assert_can_create_subscription` methods open a single
    ``session_scope`` against the injected ``async_sessionmaker`` to resolve the
    user's plan tier and (when gating) the current subscription count. The flag
    and limits are always read from the injected :class:`Settings`, so behaviour
    tracks configuration without code changes (Req 21.7).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: OverrideAwareSettings,
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            session_factory: Async session maker providing the unit-of-work
                boundary for plan-tier and subscription-count lookups.
            settings: The override-aware settings facade. The monetization flag
                and per-tier limits are resolved through its async accessors on
                every call, so a panel change applies without a bot restart
                (Req 6.4).
            logger: Optional bound logger; one is created when omitted.
        """
        self._session_factory = session_factory
        self._settings = settings
        self._log = logger or get_logger(__name__)

    # -- limit resolution ---------------------------------------------------- #

    def _limit_for_tier(
        self, limits: dict[str, PlanLimit], tier: PlanTier
    ) -> PlanLimit:
        """Return the configured :class:`PlanLimit` for ``tier``.

        Resolves the tier to its configuration key and reads it from the
        resolved ``limits`` map (passed in by the async callers after awaiting
        :meth:`OverrideAwareSettings.plan_limits`). Falls back to the Free-tier
        limit when a tier has no explicit entry so a sparse configuration never
        raises mid-gate.
        """
        key = _TIER_TO_LIMIT_KEY[tier]
        limit = limits.get(key)
        if limit is None:
            limit = limits.get(PLAN_TIER_FREE)
        if limit is None:
            # No Free entry either: treat as no allowance for this tier.
            return PlanLimit(max_subscriptions=1, notification_modes=set())
        return limit

    async def max_subscriptions(self, user: User) -> int | float:
        """Return the subscription cap for an already-loaded ``user`` (Req 21.1).

        Returns ``math.inf`` when monetization is disabled — full access,
        regardless of plan tier (Req 21.2). Otherwise returns the user's plan
        tier's configured ``max_subscriptions`` (Req 21.1, 21.3).

        Args:
            user: A user whose ``plan`` relationship is loaded. The plan is only
                read when monetization is enabled, so a user without a loaded
                plan is safe while the flag is off.
        """
        if not await self._settings.monetization_enabled():
            return math.inf
        limits = await self._settings.plan_limits()
        return self._limit_for_tier(limits, user.plan.tier).max_subscriptions

    async def max_subscriptions_for(self, user_id: int) -> int | float:
        """Return the subscription cap for ``user_id`` (Req 21.1, 21.3).

        Returns ``math.inf`` when monetization is disabled (Req 21.2). Otherwise
        resolves the user's plan tier (defaulting to Free when no plan exists)
        and returns that tier's configured ``max_subscriptions``.
        """
        if not await self._settings.monetization_enabled():
            return math.inf
        tier = await self._resolve_tier(user_id)
        limits = await self._settings.plan_limits()
        return self._limit_for_tier(limits, tier).max_subscriptions

    async def allowed_notification_modes(self, user_id: int) -> set[NotificationMode]:
        """Return the notification modes ``user_id`` may use (Req 21.5).

        Returns every :class:`NotificationMode` when monetization is disabled —
        full access (Req 21.2). Otherwise resolves the user's plan tier
        (defaulting to Free) and maps the tier's configured mode keys onto
        :class:`NotificationMode` members (Req 21.5).
        """
        if not await self._settings.monetization_enabled():
            return set(NotificationMode)
        tier = await self._resolve_tier(user_id)
        limits = await self._settings.plan_limits()
        limit = self._limit_for_tier(limits, tier)
        return {NotificationMode.from_key(key) for key in limit.notification_modes}

    async def assert_can_create_subscription(self, user_id: int) -> None:
        """Gate subscription creation for ``user_id`` (Req 21.3, 21.4).

        This coroutine is wired into
        :class:`~brizocast.services.subscription_service.SubscriptionService`'s
        ``on_before_create`` hook — the single monetization touch-point.

        * When monetization is disabled it returns immediately (no-op), so MVP
          creation is unchanged (Req 21.2, 21.6).
        * When enabled it resolves the user's plan tier and current subscription
          count in one unit of work and raises
          :class:`~brizocast.core.errors.QuotaExceededError` (carrying the tier
          limit) if creating one more would meet or exceed ``max_subscriptions``
          (Req 21.4).

        Args:
            user_id: The owning user's id.

        Raises:
            QuotaExceededError: If creating a subscription would exceed the
                user's plan-tier limit (only while monetization is enabled).
        """
        if not await self._settings.monetization_enabled():
            return

        limits = await self._settings.plan_limits()
        async with session_scope(self._session_factory) as session:
            tier = await self._resolve_tier_in_session(session, user_id)
            limit = self._limit_for_tier(limits, tier).max_subscriptions
            subscriptions = SqlAlchemySubscriptionRepository(session)
            current = await subscriptions.count_for_user(user_id)

        if current >= limit:
            self._log.info(
                "blocked subscription creation for user %s: count=%s reached "
                "tier %s limit %s",
                user_id,
                current,
                tier.value,
                limit,
            )
            raise QuotaExceededError(
                f"plan limit of {limit} subscription(s) reached",
                limit=limit,
            )

    # -- plan-tier resolution ------------------------------------------------ #

    async def _resolve_tier(self, user_id: int) -> PlanTier:
        """Resolve ``user_id``'s plan tier in its own unit of work (default Free)."""
        async with session_scope(self._session_factory) as session:
            return await self._resolve_tier_in_session(session, user_id)

    async def _resolve_tier_in_session(
        self, session: AsyncSession, user_id: int
    ) -> PlanTier:
        """Resolve ``user_id``'s plan tier within ``session``.

        Looks up the user's plan via :class:`SqlAlchemyPlanRepository`; defaults
        to :attr:`PlanTier.FREE` when the user has no plan, so a freshly-seen or
        plan-less user is always treated as Free rather than erroring.
        """
        plans = SqlAlchemyPlanRepository(session)
        plan = await plans.get_for_user(user_id)
        if plan is None:
            return PlanTier.FREE
        return plan.tier
