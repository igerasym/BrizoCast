"""Notification modes and digest selection (pure, no I/O).

Defines the :class:`NotificationMode` enum and the pure digest-selection logic
the scheduler's digest jobs use to turn a buffer of qualifying surf scores into
a ready-to-send :class:`Digest` — or *nothing at all* when a period produced no
qualifying conditions.

This module owns only the *what to summarise* decision. It performs no
persistence, no Telegram dispatch, and no time-of-day scheduling: those belong
to the digest jobs (``scheduler/digest_jobs.py``) and the sender
(``notifications/sender.py``). Keeping the selection pure makes it deterministic
and property-testable (supports Property 9).

Modes
-----
:class:`NotificationMode` mirrors :term:`Notification_Mode` (Req 10.1) and its
member *values* are the exact string keys defined in
:mod:`brizocast.config.settings` (``NOTIFICATION_MODE_*``). Because the enum
subclasses :class:`enum.StrEnum`, a member compares and serialises as its string
key, so a persisted ``subscription.notification_mode`` string round-trips to a
:class:`NotificationMode` (via :meth:`NotificationMode.from_key`) and back
without any mapping table.

Digest selection
----------------
* Morning / evening digests list every qualifying score buffered since the
  previous digest, in chronological order (Req 10.5, 10.6) — see
  :func:`select_recent`.
* The weekly-best-day digest reports the single forecast day whose maximum surf
  score is the highest in the period (Req 10.7) — see
  :func:`select_weekly_best_day`.
* When a period buffered no qualifying scores, every builder yields ``None`` so
  the caller sends no message (Req 10.8, supports Property 9).

A score is *qualifying* when its category is at or above
:attr:`~brizocast.core.domain.scoring.ScoreCategory.RIDEABLE`; sub-Rideable
items are filtered out here so the empty-period rule holds even if the engine
ever forwards a mixed buffer.

Requirements covered: 10.1, 10.5, 10.6, 10.7, 10.8 (Notification modes and
digests; supports Property 9).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from brizocast.config.settings import (
    NOTIFICATION_MODE_EVENING_DIGEST,
    NOTIFICATION_MODE_IMMEDIATE,
    NOTIFICATION_MODE_MORNING_DIGEST,
    NOTIFICATION_MODE_WEEKLY_BEST_DAY,
)
from brizocast.core.domain.scoring import ScoreCategory, ScoreResult
from brizocast.core.domain.spot import SurfSpot

__all__ = [
    "Digest",
    "DigestItem",
    "DigestPeriod",
    "NotificationMode",
    "build_digest",
    "select_recent",
    "select_weekly_best_day",
]


class NotificationMode(StrEnum):
    """How a subscription receives alerts (:term:`Notification_Mode`, Req 10.1).

    Member values are the stable string keys from
    :mod:`brizocast.config.settings`, so a member *is* its persisted key and a
    stored ``subscription.notification_mode`` maps straight onto a member with
    :meth:`from_key`.
    """

    IMMEDIATE = NOTIFICATION_MODE_IMMEDIATE
    MORNING_DIGEST = NOTIFICATION_MODE_MORNING_DIGEST
    EVENING_DIGEST = NOTIFICATION_MODE_EVENING_DIGEST
    WEEKLY_BEST_DAY = NOTIFICATION_MODE_WEEKLY_BEST_DAY

    @classmethod
    def from_key(cls, key: str) -> "NotificationMode":
        """Resolve a persisted notification-mode string to a member.

        :param key: One of the ``NOTIFICATION_MODE_*`` config string keys.
        :returns: The matching :class:`NotificationMode`.
        :raises ValueError: If ``key`` is not a recognised notification mode.
        """

        return cls(key)

    @property
    def is_digest(self) -> bool:
        """Whether this mode is delivered as a scheduled digest.

        ``True`` for the morning, evening, and weekly-best-day digests; ``False``
        for :attr:`IMMEDIATE`, which dispatches as conditions are detected.
        """

        return self is not NotificationMode.IMMEDIATE


class DigestPeriod(BaseModel):
    """The time span a digest summarises (e.g. since the previous digest).

    Carried on the produced :class:`Digest` as metadata for the message header
    (e.g. "Morning digest for ..."); the selection logic does not filter items
    by this span — buffering qualifying scores for a period is the engine's job.
    """

    model_config = ConfigDict(frozen=True)

    start: datetime = Field(description="Inclusive start of the digest period.")
    end: datetime = Field(description="Inclusive end of the digest period.")


class DigestItem(BaseModel):
    """One qualifying surf score in a digest, paired with its surf spot.

    Binds a :class:`~brizocast.core.domain.spot.SurfSpot` to the
    :class:`~brizocast.core.domain.scoring.ScoreResult` computed for it so the
    digest formatter can render both the spot and the score/breakdown.
    """

    model_config = ConfigDict(frozen=True)

    spot: SurfSpot = Field(description="The surf spot the score was computed for.")
    score_result: ScoreResult = Field(description="The qualifying score for the spot.")

    @property
    def surf_score(self) -> int:
        """The integer surf score (``0..100``) of this item."""

        return self.score_result.score

    @property
    def timestamp(self) -> datetime:
        """The start of the score's forecast window (its chronological position)."""

        return self.score_result.forecast_window.start


class Digest(BaseModel):
    """A non-empty digest ready for the sender to deliver.

    A :class:`Digest` is only ever constructed when there is something to send:
    :attr:`items` always holds at least one :class:`DigestItem`. An empty period
    is represented by the builders returning ``None`` instead (Req 10.8).
    """

    model_config = ConfigDict(frozen=True)

    mode: NotificationMode = Field(description="The digest mode that produced this summary.")
    period: DigestPeriod = Field(description="The time span this digest summarises.")
    items: list[DigestItem] = Field(
        min_length=1, description="The qualifying items to report (never empty)."
    )


def _is_qualifying(item: DigestItem) -> bool:
    """Whether an item's score qualifies for a digest (category ≥ Rideable)."""

    return item.score_result.category >= ScoreCategory.RIDEABLE


def _utc_day(moment: datetime) -> date:
    """Return the UTC calendar day of ``moment`` (naive values treated as UTC)."""

    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC).date()
    return moment.astimezone(UTC).date()


def _sort_key(item: DigestItem) -> tuple[datetime, str]:
    """Deterministic ordering: chronological, then by spot key for ties."""

    return (item.timestamp, item.spot.spot_key)


def select_recent(items: list[DigestItem]) -> list[DigestItem]:
    """Select the items for a morning or evening digest (Req 10.5, 10.6).

    Returns every *qualifying* item, in chronological order (ties broken by spot
    key for determinism). Sub-Rideable items are dropped, so a buffer with no
    qualifying scores yields an empty list — the signal for "send nothing".

    :param items: The buffer of scores accumulated since the previous digest.
    :returns: The qualifying items to list, ordered chronologically (possibly
        empty).
    """

    qualifying = [item for item in items if _is_qualifying(item)]
    return sorted(qualifying, key=_sort_key)


def select_weekly_best_day(items: list[DigestItem]) -> list[DigestItem]:
    """Select the items of the best forecast day for a weekly digest (Req 10.7).

    Groups qualifying items by UTC calendar day, finds the day whose *maximum*
    surf score is the highest in the period, and returns that day's items in
    chronological order. Ties on the maximum score are broken by the earliest
    day, so the result is deterministic.

    :param items: The buffer of scores accumulated over the week.
    :returns: The chosen day's qualifying items, ordered chronologically; empty
        when no item qualifies.
    """

    qualifying = [item for item in items if _is_qualifying(item)]
    if not qualifying:
        return []

    by_day: dict[date, list[DigestItem]] = {}
    for item in qualifying:
        by_day.setdefault(_utc_day(item.timestamp), []).append(item)

    # Pick the day with the highest max score; ties resolved by the earliest day
    # (min on the day) so selection is stable and reproducible.
    best_day = max(
        by_day,
        key=lambda day: (max(i.surf_score for i in by_day[day]), -day.toordinal()),
    )
    return sorted(by_day[best_day], key=_sort_key)


def build_digest(
    mode: NotificationMode,
    items: list[DigestItem],
    period: DigestPeriod,
) -> Digest | None:
    """Build the digest for ``mode``, or ``None`` when there is nothing to send.

    Routes to :func:`select_recent` for the morning/evening modes and to
    :func:`select_weekly_best_day` for the weekly-best-day mode. When the
    selection is empty (no qualifying scores in the period), returns ``None`` so
    the caller dispatches no message (Req 10.8, supports Property 9).

    :param mode: A digest mode; :attr:`NotificationMode.IMMEDIATE` is rejected
        because immediate alerts are not digest-driven.
    :param items: The qualifying-score buffer for the period.
    :param period: The time span the digest summarises (carried as metadata).
    :returns: A non-empty :class:`Digest`, or ``None`` for an empty period.
    :raises ValueError: If ``mode`` is :attr:`NotificationMode.IMMEDIATE`.
    """

    if mode is NotificationMode.IMMEDIATE:
        raise ValueError("immediate mode is not digest-driven; build_digest requires a digest mode")

    if mode is NotificationMode.WEEKLY_BEST_DAY:
        selected = select_weekly_best_day(items)
    else:
        selected = select_recent(items)

    if not selected:
        return None
    return Digest(mode=mode, period=period, items=selected)
