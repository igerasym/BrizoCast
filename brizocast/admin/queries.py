"""Read-side query helpers for the admin panel's list/detail/stats views.

These functions back the panel's read-only pages (users, subscriptions,
feedback, stats). They run plain aggregate/join ``SELECT`` queries over the
shared database through the panel container's ``async_sessionmaker`` and return
small, immutable, presentation-ready value objects — so the routers stay thin
HTTP adapters and never touch ORM relationships directly.

They intentionally live in the admin package (not the bot's repository layer):
the bot never needs these cross-entity reporting reads, and keeping them here
avoids widening the bot's repositories with panel-only queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.database.session import session_scope
from brizocast.models.activity import Activity
from brizocast.models.feedback import Feedback, FeedbackRating
from brizocast.models.location import Location
from brizocast.models.plan import Plan
from brizocast.models.subscription import Subscription
from brizocast.models.user import User

__all__ = [
    "FeedbackRow",
    "FeedbackView",
    "StatsView",
    "SubscriptionRow",
    "UserDetailView",
    "UserRow",
    "get_user_detail",
    "list_feedback",
    "list_subscriptions",
    "list_users",
    "tier_counts",
]


def _location_label(location: Location) -> str:
    """Return a short, user-facing label for ``location``."""
    if location.label:
        return location.label
    if location.city:
        return location.city
    return f"{location.lat:.4f}, {location.lon:.4f}"


def _location_place(location: Location) -> str:
    """Return a longer "City, Country" place string for ``location``."""
    parts = [part for part in (location.city, location.country) if part]
    if parts:
        return ", ".join(parts)
    return f"{location.lat:.4f}, {location.lon:.4f}"


@dataclass(frozen=True)
class UserRow:
    """One row of the users list: identity, plan tier, and subscription count."""

    id: int
    telegram_user_id: int
    username: str | None
    plan_tier: str
    subscription_count: int


@dataclass(frozen=True)
class UserDetailView:
    """A user's profile and plan for the detail page (subscriptions added separately)."""

    id: int
    telegram_user_id: int
    username: str | None
    onboarded: bool
    selected_activity_key: str | None
    created_at: datetime
    plan_tier: str | None
    plan_status: str | None
    plan_expiry_at: datetime | None


@dataclass(frozen=True)
class SubscriptionRow:
    """One row of the all-subscriptions list."""

    id: int
    owner_telegram_id: int
    activity: str
    location_label: str
    location_place: str
    location_lat: float
    location_lon: float
    search_radius_km: float
    notification_mode: str


@dataclass(frozen=True)
class FeedbackRow:
    """One row of the feedback list."""

    id: int
    owner_telegram_id: int | None
    spot_key: str
    surf_score: int
    rating: str
    created_at: datetime


@dataclass(frozen=True)
class FeedbackView:
    """The feedback list plus the thumbs-up / thumbs-down totals (Req 10.2)."""

    rows: list[FeedbackRow]
    up_count: int
    down_count: int


@dataclass(frozen=True)
class StatsView:
    """Aggregate counts for the dashboard / stats page (Req 11.1)."""

    total_users: int
    tier_counts: dict[str, int]
    total_subscriptions: int
    total_spots: int
    last_scheduler_run: datetime | None


async def list_users(session_factory: async_sessionmaker[AsyncSession]) -> list[UserRow]:
    """Return one :class:`UserRow` per user with plan tier and sub count (Req 2.1)."""
    async with session_scope(session_factory) as session:
        sub_count = func.count(Subscription.id)
        stmt = (
            select(
                User.id,
                User.telegram_user_id,
                User.username,
                Plan.tier,
                sub_count,
            )
            .select_from(User)
            .outerjoin(Plan, Plan.user_id == User.id)
            .outerjoin(Subscription, Subscription.user_id == User.id)
            .group_by(User.id, Plan.tier)
            .order_by(User.id)
        )
        result = await session.execute(stmt)
        rows: list[UserRow] = []
        for uid, tg_id, username, tier, count in result.all():
            rows.append(
                UserRow(
                    id=uid,
                    telegram_user_id=tg_id,
                    username=username,
                    plan_tier=str(tier) if tier is not None else "—",
                    subscription_count=int(count),
                )
            )
        return rows


async def get_user_detail(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> UserDetailView | None:
    """Return a user's profile + plan, or ``None`` if no such user (Req 2.2, 2.5)."""
    async with session_scope(session_factory) as session:
        user = await session.get(User, user_id)
        if user is None:
            return None
        plan = (
            await session.execute(select(Plan).where(Plan.user_id == user_id))
        ).scalar_one_or_none()
        return UserDetailView(
            id=user.id,
            telegram_user_id=user.telegram_user_id,
            username=user.username,
            onboarded=user.onboarded,
            selected_activity_key=user.selected_activity_key,
            created_at=user.created_at,
            plan_tier=str(plan.tier) if plan is not None else None,
            plan_status=str(plan.status) if plan is not None else None,
            plan_expiry_at=plan.expiry_at if plan is not None else None,
        )


async def list_subscriptions(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[SubscriptionRow]:
    """Return every subscription with owner, activity, location, radius, mode (Req 3.1)."""
    async with session_scope(session_factory) as session:
        stmt = (
            select(Subscription, User.telegram_user_id, Activity.display_name, Location)
            .join(User, User.id == Subscription.user_id)
            .join(Activity, Activity.id == Subscription.activity_id)
            .join(Location, Location.id == Subscription.location_id)
            .order_by(Subscription.id)
        )
        result = await session.execute(stmt)
        rows: list[SubscriptionRow] = []
        for sub, owner_tg, activity_name, location in result.all():
            rows.append(
                SubscriptionRow(
                    id=sub.id,
                    owner_telegram_id=owner_tg,
                    activity=activity_name,
                    location_label=_location_label(location),
                    location_place=_location_place(location),
                    location_lat=location.lat,
                    location_lon=location.lon,
                    search_radius_km=sub.search_radius_km,
                    notification_mode=sub.notification_mode,
                )
            )
        return rows


async def list_feedback(
    session_factory: async_sessionmaker[AsyncSession],
) -> FeedbackView:
    """Return all feedback entries (newest first) plus up/down totals (Req 10.1, 10.2)."""
    async with session_scope(session_factory) as session:
        stmt = (
            select(Feedback, User.telegram_user_id)
            .join(Subscription, Subscription.id == Feedback.subscription_id)
            .join(User, User.id == Subscription.user_id)
            .order_by(Feedback.created_at.desc(), Feedback.id.desc())
        )
        result = await session.execute(stmt)
        rows: list[FeedbackRow] = []
        up = 0
        down = 0
        for feedback, owner_tg in result.all():
            rows.append(
                FeedbackRow(
                    id=feedback.id,
                    owner_telegram_id=owner_tg,
                    spot_key=feedback.spot_key,
                    surf_score=feedback.surf_score,
                    rating=str(feedback.rating),
                    created_at=feedback.created_at,
                )
            )
            if feedback.rating == FeedbackRating.UP:
                up += 1
            else:
                down += 1
        return FeedbackView(rows=rows, up_count=up, down_count=down)


async def tier_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, dict[str, int], int]:
    """Return ``(total_users, {tier: count}, total_subscriptions)`` (Req 11.1)."""
    async with session_scope(session_factory) as session:
        total_users = int(
            (await session.execute(select(func.count()).select_from(User))).scalar_one()
        )
        total_subs = int(
            (
                await session.execute(select(func.count()).select_from(Subscription))
            ).scalar_one()
        )
        tier_rows = await session.execute(
            select(Plan.tier, func.count(Plan.id)).group_by(Plan.tier)
        )
        counts = {str(tier): int(count) for tier, count in tier_rows.all()}
        return total_users, counts, total_subs
