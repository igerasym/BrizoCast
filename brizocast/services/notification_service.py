"""Notification-record persistence and forecast-window identity service.

``NotificationService`` is the application-layer use case that **persists the
history of dispatched alerts** and exposes the anti-spam / digest lookups over
that history. It is deliberately narrow: it owns *persistence + window identity*
only. The decision of *whether* to send (anti-spam, mute, snooze, quiet hours)
lives in the pure :mod:`brizocast.core.domain.antispam` policy and the
:mod:`brizocast.notifications.engine`; the actual Telegram delivery lives in
:mod:`brizocast.notifications.sender`.

Persistence happens **after a successful dispatch**: the ``NotificationEngine``
(task 5.1) calls :meth:`record_sent` once an alert has been delivered, so the
stored :class:`~brizocast.models.notification.NotificationSent` row faithfully
reflects the alert that went out — same subscription, spot, surf score,
forecast window, and a send timestamp (Req 9.2; Property 7).

Methods
-------
* :meth:`record_sent` — persist a ``NotificationSent`` from a dispatched
  :class:`~brizocast.core.domain.scoring.ScoreResult` (Req 9.2).
* :meth:`latest_for_window` — most recent record for the
  ``(subscription, spot, forecast window)`` identity, the anti-spam lookup
  (Req 9.3-9.5).
* :meth:`records_since` — records for a subscription since a cutoff, used to
  assemble digests (Req 10.2).

Session strategy
----------------
The service is injected with an ``async_sessionmaker`` and opens a fresh
unit-of-work per call via
:func:`brizocast.database.session.session_scope`, constructing a
:class:`~brizocast.repositories.notification_repo.SqlAlchemyNotificationRepository`
over that session. Each call commits independently — appropriate because a sent
alert and its record are not part of a larger transaction with the caller.

The forecast-window dedup identity is derived through
:func:`brizocast.notifications.window.window_key`, keeping it consistent with
the engine.

Requirements covered: 9.2, 10.2 (supports Property 7).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.core.domain.scoring import ScoreResult
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.database.session import session_scope
from brizocast.models.notification import NotificationSent
from brizocast.notifications.window import window_key
from brizocast.repositories.notification_repo import SqlAlchemyNotificationRepository

__all__ = ["NotificationService"]


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class NotificationService:
    """Persists sent-alert records and serves anti-spam / digest lookups."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            session_factory: Factory used to open a unit-of-work session per
                call (typically built by
                :func:`brizocast.database.session.create_session_factory`).
            logger: Optional bound logger; one is created when omitted.
        """
        self._session_factory = session_factory
        self._log = logger or get_logger(__name__)

    async def record_sent(
        self,
        subscription_id: int,
        spot_key: str,
        score_result: ScoreResult,
        *,
        sent_at: datetime | None = None,
    ) -> NotificationSent:
        """Persist a record of a dispatched alert and return it (Req 9.2).

        Called by the notification engine **after** a successful send. The
        stored row carries the same subscription, spot, surf score, and forecast
        window (key, start, end) as the dispatched alert, plus the send
        timestamp — so the persisted history faithfully reflects what went out
        (Property 7).

        Args:
            subscription_id: The subscription the alert was sent for.
            spot_key: The surf spot the alert concerned.
            score_result: The scored result that was dispatched; supplies the
                surf score and the forecast window identity.
            sent_at: The dispatch timestamp; defaults to the current UTC time.

        Returns:
            The persisted :class:`NotificationSent` with its primary key set.
        """
        window = score_result.forecast_window
        timestamp = sent_at if sent_at is not None else _utc_now()
        record = NotificationSent(
            subscription_id=subscription_id,
            spot_key=spot_key,
            surf_score=score_result.score,
            forecast_window_key=window_key(window),
            forecast_window_start=window.start,
            forecast_window_end=window.end,
            sent_at=timestamp,
        )
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyNotificationRepository(session, logger=self._log)
            stored = await repo.add(record)
        self._log.bind(
            subscription_id=subscription_id, spot_key=spot_key
        ).info(
            "recorded sent alert (score=%s, window=%s)",
            stored.surf_score,
            stored.forecast_window_key,
        )
        return stored

    async def latest_for_window(
        self, subscription_id: int, spot_key: str, forecast_window_key: str
    ) -> NotificationSent | None:
        """Return the most recent record for the dedup identity (Req 9.3-9.5).

        Wraps the repository's ``latest`` lookup over the
        ``(subscription, spot, forecast window)`` identity used by the anti-spam
        policy to compare a candidate score against the last alert sent for the
        same window.

        Args:
            subscription_id: The subscription to look up.
            spot_key: The surf spot to look up.
            forecast_window_key: The forecast-window dedup key (see
                :func:`brizocast.notifications.window.window_key`).

        Returns:
            The newest matching :class:`NotificationSent`, or ``None`` when no
            alert has been recorded for that identity.
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyNotificationRepository(session, logger=self._log)
            return await repo.latest(
                subscription_id, spot_key, forecast_window_key
            )

    async def records_since(
        self, subscription_id: int, since: datetime
    ) -> list[NotificationSent]:
        """Return a subscription's records sent at/after ``since`` (Req 10.2).

        Used to assemble digests: the records dispatched for a subscription
        since the previous digest period.

        Args:
            subscription_id: The subscription whose history to read.
            since: Inclusive lower bound on ``sent_at``.

        Returns:
            Matching records ordered oldest-first by send timestamp.
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyNotificationRepository(session, logger=self._log)
            return await repo.list_since(subscription_id, since)
