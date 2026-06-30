"""User provisioning service: lookup/create users and their Free plan.

``UserService`` is the application-layer use case that turns a Telegram user
identifier into a persisted :class:`~brizocast.models.user.User`, provisioning
the user's one-and-only monetization :class:`~brizocast.models.plan.Plan` at
the same time. It is the single entry point the bot's onboarding flow (task
7.2) uses on a user's *first interaction* (Req 1.7).

Provisioning contract (Req 1.7, 20.1, 20.3, 20.4; supports Property 21)
-----------------------------------------------------------------------
:meth:`UserService.get_or_create_user` is **idempotent** and keyed by the
Telegram user id:

* On the first call for an id, it creates exactly one ``User`` row **and**, in
  the *same transaction*, exactly one ``Plan`` row with
  ``tier=Free``/``status=active``/``start_at=now`` and a ``NULL`` expiry (Free
  plans never expire). Creating both inside one unit of work makes the pair
  all-or-nothing: a user can never be persisted without its Free active plan.
* On every subsequent call for the same id, it returns the existing user
  unchanged (no second user, no second plan).

The result is the invariant in **Property 21**: for any sequence of first
interactions, exactly one user exists per Telegram id, associated with exactly
one Free, active plan.

Session / transaction strategy
------------------------------
Following the repository layer's documented unit-of-work boundary
(see :mod:`brizocast.repositories.base`), this service is injected with the
``async_sessionmaker`` and, for each use case, opens **one** session via
:func:`brizocast.database.session.session_scope`, shares it across the user and
plan repositories, and lets the context manager commit (or roll back) the whole
operation atomically. The repositories stay thin and transaction-free; the
service owns the unit of work.

A monotonic ``now`` clock is injected so the plan's ``start_at`` is
deterministic in tests.

Requirements covered: 1.7, 20.1, 20.2, 20.3, 20.4 (supports Property 21).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.core.errors import NotFoundError
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.database.session import session_scope
from brizocast.models.plan import Plan, PlanStatus, PlanTier
from brizocast.models.subscription import Subscription
from brizocast.models.user import User
from brizocast.repositories.plan_repo import SqlAlchemyPlanRepository
from brizocast.repositories.user_repo import SqlAlchemyUserRepository

__all__ = ["UserListItem", "UserProfile", "UserService"]


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class UserListItem:
    """Presentation-ready row for the admin users list (Req 2.1).

    Carries everything the panel's ``/users`` view needs to render one line —
    the internal id (used to link to the detail page), the Telegram user
    identifier, the current plan tier (``None`` when a user somehow has no
    plan), and the count of subscriptions the user owns — without the caller
    touching ORM rows or opening a session.
    """

    user_id: int
    telegram_user_id: int
    plan_tier: str | None
    subscription_count: int


@dataclass(frozen=True, slots=True)
class UserProfile:
    """Presentation-ready detail view of a single user (Req 2.2).

    Projects the user's profile fields together with the current plan tier and
    status so the panel's ``/users/{id}`` page can render them without lazy ORM
    relationship access.
    """

    user_id: int
    telegram_user_id: int
    username: str | None
    onboarded: bool
    selected_activity_key: str | None
    plan_tier: str | None
    plan_status: str | None


class UserService:
    """Lookup/create users and provision their Free active plan (Req 1.7, 20.*)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime] = _utc_now,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            session_factory: The application's ``async_sessionmaker``. Each use
                case opens one session from it via ``session_scope`` so user and
                plan writes share a single transaction.
            now: Clock returning the current time; injected for testability. Used
                as a new plan's ``start_at``.
            logger: Optional bound logger; one is created when omitted.
        """
        self._session_factory = session_factory
        self._now = now
        self._log = logger or get_logger(__name__)

    async def get_or_create_user(
        self,
        telegram_user_id: int,
        username: str | None = None,
    ) -> User:
        """Return the user for ``telegram_user_id``, creating it if absent.

        Idempotent and keyed by the Telegram id (Req 1.7). On first creation the
        user is provisioned with exactly one Free, active plan
        (``start_at=now``) in the same transaction (Req 20.1, 20.3, 20.4). On
        subsequent calls the existing user is returned unchanged — no duplicate
        user and no duplicate plan are created (supports Property 21).

        Args:
            telegram_user_id: The unique Telegram user identifier.
            username: Optional Telegram username, stored only when the user is
                first created.

        Returns:
            The persisted :class:`~brizocast.models.user.User` (existing or
            newly created).
        """
        try:
            async with session_scope(self._session_factory) as session:
                users = SqlAlchemyUserRepository(session)
                existing = await users.get_by_telegram_id(telegram_user_id)
                if existing is not None:
                    return existing
                return await self._create_user_with_plan(
                    session, telegram_user_id, username
                )
        except IntegrityError:
            # A concurrent first-interaction won the race and inserted the user
            # between our read and insert (the ``telegram_user_id`` unique
            # constraint fired); ``session_scope`` rolled this transaction back.
            # Re-read in a fresh transaction so the call still returns the
            # single canonical user, preserving idempotency (Property 21).
            self._log.debug("user creation raced; re-reading existing user")

        async with session_scope(self._session_factory) as session:
            raced = await SqlAlchemyUserRepository(session).get_by_telegram_id(
                telegram_user_id
            )
            if raced is not None:
                return raced
            # The conflict was not the expected telegram-id collision; recreate
            # so the failure surfaces rather than returning ``None``.
            return await self._create_user_with_plan(
                session, telegram_user_id, username
            )

    async def _create_user_with_plan(
        self,
        session: AsyncSession,
        telegram_user_id: int,
        username: str | None,
    ) -> User:
        """Create a user and its Free active plan within ``session``.

        Both inserts share the caller's transaction so the user and its plan are
        committed atomically (Req 20.1, 20.3, 20.4).
        """
        users = SqlAlchemyUserRepository(session)
        plans = SqlAlchemyPlanRepository(session)

        user = await users.add(
            User(telegram_user_id=telegram_user_id, username=username)
        )
        await plans.add(
            Plan(
                user_id=user.id,
                tier=PlanTier.FREE,
                status=PlanStatus.ACTIVE,
                start_at=self._now(),
                expiry_at=None,
            )
        )
        self._log.info(
            "provisioned new user with Free active plan",
        )
        return user

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        """Return the user with the given Telegram id, or ``None`` if absent."""
        async with session_scope(self._session_factory) as session:
            return await SqlAlchemyUserRepository(session).get_by_telegram_id(
                telegram_user_id
            )

    async def mark_onboarded(self, telegram_user_id: int) -> User:
        """Mark the user's onboarding complete and return the updated user.

        Used once the onboarding conversation finishes so a later ``/start``
        shows the main menu instead of repeating activity selection (Req 1.6).

        Raises:
            ValueError: If no user exists for ``telegram_user_id``.
        """
        async with session_scope(self._session_factory) as session:
            users = SqlAlchemyUserRepository(session)
            user = await users.get_by_telegram_id(telegram_user_id)
            if user is None:
                raise ValueError(f"unknown telegram user id: {telegram_user_id}")
            user.onboarded = True
            await users.update(user)
            return user

    async def set_selected_activity(
        self,
        telegram_user_id: int,
        activity_key: str,
    ) -> User:
        """Persist the user's selected activity and return the updated user.

        Records the activity chosen during onboarding (Req 1.5) on the user row.

        Raises:
            ValueError: If no user exists for ``telegram_user_id``.
        """
        async with session_scope(self._session_factory) as session:
            users = SqlAlchemyUserRepository(session)
            user = await users.get_by_telegram_id(telegram_user_id)
            if user is None:
                raise ValueError(f"unknown telegram user id: {telegram_user_id}")
            user.selected_activity_key = activity_key
            await users.update(user)
            return user

    # ------------------------------------------------------------------ #
    # Admin-panel read/write use cases (Req 2.1, 2.2, 2.3). Thin methods
    # owning a single unit of work each, returning session-free value
    # objects so the panel routers stay thin HTTP adapters.
    # ------------------------------------------------------------------ #

    async def list_overview(self) -> list[UserListItem]:
        """Return every user with its plan tier and subscription count (Req 2.1).

        Resolves all three columns in a single grouped query (left-joining the
        one-to-one plan and counting owned subscriptions) so the admin users
        list renders without N+1 lookups. Users are returned in stable id order.
        """
        async with session_scope(self._session_factory) as session:
            stmt = (
                select(
                    User.id,
                    User.telegram_user_id,
                    Plan.tier,
                    func.count(Subscription.id),
                )
                .outerjoin(Plan, Plan.user_id == User.id)
                .outerjoin(Subscription, Subscription.user_id == User.id)
                .group_by(User.id, User.telegram_user_id, Plan.tier)
                .order_by(User.id)
            )
            result = await session.execute(stmt)
            items: list[UserListItem] = []
            for user_id, telegram_user_id, tier, sub_count in result.all():
                items.append(
                    UserListItem(
                        user_id=int(user_id),
                        telegram_user_id=int(telegram_user_id),
                        plan_tier=None if tier is None else str(tier.value),
                        subscription_count=int(sub_count),
                    )
                )
            return items

    async def get_profile(self, user_id: int) -> UserProfile | None:
        """Return the user's profile and plan, or ``None`` if absent (Req 2.2, 2.5).

        Looks the user up by internal id; returns ``None`` when no such user
        exists so the router can respond 404 (Req 2.5).
        """
        async with session_scope(self._session_factory) as session:
            user = await SqlAlchemyUserRepository(session).get(user_id)
            if user is None:
                return None
            plan = await SqlAlchemyPlanRepository(session).get_for_user(user_id)
            return UserProfile(
                user_id=user.id,
                telegram_user_id=user.telegram_user_id,
                username=user.username,
                onboarded=user.onboarded,
                selected_activity_key=user.selected_activity_key,
                plan_tier=None if plan is None else plan.tier.value,
                plan_status=None if plan is None else plan.status.value,
            )

    async def set_plan_tier(self, user_id: int, tier: PlanTier) -> Plan:
        """Set the user's plan tier and persist it, returning the plan (Req 2.3).

        Args:
            user_id: The internal id of the user whose plan to change.
            tier: The new :class:`~brizocast.models.plan.PlanTier` (Free or Paid).

        Returns:
            The updated :class:`~brizocast.models.plan.Plan`.

        Raises:
            NotFoundError: If no user with ``user_id`` exists, or the user has no
                plan row, so the router can surface a 404 / error rather than
                silently confirming a no-op.
        """
        async with session_scope(self._session_factory) as session:
            user = await SqlAlchemyUserRepository(session).get(user_id)
            if user is None:
                raise NotFoundError(f"user {user_id} does not exist")
            plans = SqlAlchemyPlanRepository(session)
            plan = await plans.get_for_user(user_id)
            if plan is None:
                raise NotFoundError(f"user {user_id} has no plan")
            plan.tier = tier
            await plans.update(plan)
            self._log.info("set user %s plan tier to %s", user_id, tier.value)
            return plan
