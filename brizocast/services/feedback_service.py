"""Feedback persistence service for 👍/👎 responses to alerts.

``FeedbackService`` is the application-layer use case the bot's feedback
callback handler (task 7.9) calls when a user taps the 👍/👎 inline controls on
an alert. It persists a :class:`~brizocast.models.feedback.Feedback` row — tying
the rating to the originating subscription, surf spot, and surf score — through
:class:`~brizocast.repositories.feedback_repo.SqlAlchemyFeedbackRepository`
(Req 12.4). Stored feedback is retained for preset tuning and future scoring use
(Req 12.5).

Session / transaction strategy
------------------------------
Following the repository layer's unit-of-work boundary (see
:mod:`brizocast.repositories.base`), the service is injected with the
application's ``async_sessionmaker`` and opens **one** session per use case via
:func:`brizocast.database.session.session_scope`, which commits on success and
rolls back on error. The repository stays thin and transaction-free.

Requirements covered: 12.4, 12.5 (supports Property 27).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.core.logging import BoundLogger, get_logger
from brizocast.database.session import session_scope
from brizocast.models.feedback import Feedback, FeedbackRating
from brizocast.models.subscription import Subscription
from brizocast.models.user import User
from brizocast.repositories.feedback_repo import SqlAlchemyFeedbackRepository

__all__ = ["FeedbackListItem", "FeedbackRatingCounts", "FeedbackService"]


@dataclass(frozen=True, slots=True)
class FeedbackListItem:
    """Presentation-ready row for the admin feedback view (Req 10.1).

    Projects one :class:`~brizocast.models.feedback.Feedback` entry together
    with the owning user's Telegram identifier (resolved by joining
    ``Feedback -> Subscription -> User``) so the panel's ``/feedback`` view can
    render a line without touching ORM rows or opening a session.
    """

    feedback_id: int
    telegram_user_id: int
    spot_key: str
    surf_score: int
    rating: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FeedbackRatingCounts:
    """Aggregate thumbs-up / thumbs-down totals for the feedback view (Req 10.2)."""

    up: int
    down: int


class FeedbackService:
    """Persists user thumbs-up/down feedback on alerts (Req 12.4, 12.5)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            session_factory: The application's ``async_sessionmaker``. Each use
                case opens one session from it via ``session_scope``.
            logger: Optional bound logger; one is created when omitted.
        """
        self._session_factory = session_factory
        self._log = logger or get_logger(__name__)

    async def record_feedback(
        self,
        subscription_id: int,
        spot_key: str,
        surf_score: int,
        rating: FeedbackRating | str,
    ) -> Feedback:
        """Persist a feedback entry for an alert and return the stored row.

        Associates the rating with the subscription, surf spot, and surf score
        of the alert it responds to (Req 12.4). The persisted feedback is
        retained for preset tuning and future scoring use (Req 12.5).

        Args:
            subscription_id: The subscription whose alert was rated.
            spot_key: The surf spot the rated alert concerned.
            surf_score: The surf score shown in the rated alert.
            rating: The user's rating, either a
                :class:`~brizocast.models.feedback.FeedbackRating` or its string
                value (``"up"``/``"down"``).

        Returns:
            The persisted :class:`~brizocast.models.feedback.Feedback`.

        Raises:
            ValueError: If ``rating`` is a string that is not a valid
                :class:`FeedbackRating` value.
        """
        resolved = rating if isinstance(rating, FeedbackRating) else FeedbackRating(rating)
        async with session_scope(self._session_factory) as session:
            repository = SqlAlchemyFeedbackRepository(session)
            feedback = await repository.add(
                Feedback(
                    subscription_id=subscription_id,
                    spot_key=spot_key,
                    surf_score=surf_score,
                    rating=resolved,
                )
            )
            self._log.bind(subscription_id=subscription_id, spot_key=spot_key).info(
                "recorded %s feedback (score=%d)", resolved.value, surf_score
            )
            return feedback

    async def list_for_subscription(self, subscription_id: int) -> list[Feedback]:
        """Return all feedback recorded for ``subscription_id`` (Req 12.5)."""
        async with session_scope(self._session_factory) as session:
            repository = SqlAlchemyFeedbackRepository(session)
            return await repository.list_for_subscription(subscription_id)

    # ------------------------------------------------------------------ #
    # Admin-panel read use cases (Req 10.1, 10.2). Thin methods owning a
    # single unit of work each, returning session-free value objects so the
    # panel router stays a thin HTTP adapter.
    # ------------------------------------------------------------------ #

    async def list_all(self) -> list[FeedbackListItem]:
        """Return every feedback entry with its owning user's id (Req 10.1).

        Joins ``Feedback -> Subscription -> User`` so each row carries the
        Telegram identifier of the user who left the feedback, alongside the
        surf spot, surf score, rating, and timestamp. Entries are returned newest
        first so the admin view shows the most recent feedback at the top.
        """
        async with session_scope(self._session_factory) as session:
            stmt = (
                select(
                    Feedback.id,
                    User.telegram_user_id,
                    Feedback.spot_key,
                    Feedback.surf_score,
                    Feedback.rating,
                    Feedback.created_at,
                )
                .join(Subscription, Subscription.id == Feedback.subscription_id)
                .join(User, User.id == Subscription.user_id)
                .order_by(Feedback.created_at.desc(), Feedback.id.desc())
            )
            result = await session.execute(stmt)
            return [
                FeedbackListItem(
                    feedback_id=int(feedback_id),
                    telegram_user_id=int(telegram_user_id),
                    spot_key=str(spot_key),
                    surf_score=int(surf_score),
                    rating=rating.value,
                    created_at=created_at,
                )
                for (
                    feedback_id,
                    telegram_user_id,
                    spot_key,
                    surf_score,
                    rating,
                    created_at,
                ) in result.all()
            ]

    async def rating_counts(self) -> FeedbackRatingCounts:
        """Return the total thumbs-up and thumbs-down feedback counts (Req 10.2).

        Resolves both totals in a single grouped query; ratings with no entries
        default to zero.
        """
        async with session_scope(self._session_factory) as session:
            stmt = select(Feedback.rating, func.count(Feedback.id)).group_by(
                Feedback.rating
            )
            result = await session.execute(stmt)
            counts = {rating: int(total) for rating, total in result.all()}
            return FeedbackRatingCounts(
                up=counts.get(FeedbackRating.UP, 0),
                down=counts.get(FeedbackRating.DOWN, 0),
            )
