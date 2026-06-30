"""Digest message renderer (pure function, no I/O).

Renders the human-readable text of a notification :class:`~brizocast.notifications.modes.Digest`
— the single summary message a scheduled digest job sends to a subscription
(Req 10.5, 10.6, 10.7). The function is pure and presentation-only: it reads the
already-selected items off the :class:`Digest` and never decides *what* to
include (that is :func:`brizocast.notifications.modes.build_digest`'s job) nor
performs any persistence or Telegram dispatch.

Layout
------
* A mode-specific header (e.g. "🌅 Morning digest").
* For the morning and evening digests: one line per qualifying item listing the
  surf spot, the surf score and its category, and the forecast window, in the
  chronological order :func:`select_recent` produced (Req 10.5, 10.6).
* For the weekly-best-day digest: a header naming the chosen day followed by
  that day's items, in the order :func:`select_weekly_best_day` produced
  (Req 10.7).

Because a :class:`Digest` is only ever constructed for a non-empty period
(:func:`build_digest` returns ``None`` otherwise — Req 10.8), this function
always renders at least one item line and is never asked to format an empty
summary.

Requirements covered: 10.5, 10.6, 10.7.
"""

from __future__ import annotations

from datetime import UTC, datetime

from brizocast.core.domain.forecast import ForecastWindow
from brizocast.notifications.modes import Digest, DigestItem, NotificationMode

__all__ = ["format_digest"]


# Mode-specific message headers.
_HEADERS: dict[NotificationMode, str] = {
    NotificationMode.MORNING_DIGEST: "🌅 Morning digest",
    NotificationMode.EVENING_DIGEST: "🌆 Evening digest",
    NotificationMode.WEEKLY_BEST_DAY: "📅 Best surf day this week",
}


def _category_label(item: DigestItem) -> str:
    """Render an item's score category as a title-cased word (e.g. ``"Good"``)."""

    return item.score_result.category.name.title()


def _to_utc(value: datetime) -> datetime:
    """Normalize a datetime to UTC, treating naive values as already-UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_window(window: ForecastWindow) -> str:
    """Render a forecast window as a readable UTC time range.

    A single-instant window (``start == end``) renders as one timestamp; an
    interval renders ``start–end``, dropping the repeated date when both bounds
    fall on the same UTC day. Times are normalized to UTC for determinism.
    """

    start = _to_utc(window.start)
    end = _to_utc(window.end)
    start_text = start.strftime("%Y-%m-%d %H:%M")
    if start == end:
        return f"{start_text} UTC"
    if start.date() == end.date():
        return f"{start_text}\u2013{end.strftime('%H:%M')} UTC"
    return f"{start_text}\u2013{end.strftime('%Y-%m-%d %H:%M')} UTC"


def _format_item(item: DigestItem) -> str:
    """Render one digest line: spot, score (category), and forecast window."""

    return (
        f"🏄 {item.spot.name}"
        f" — Score {item.surf_score} ({_category_label(item)})"
        f" · {_format_window(item.score_result.forecast_window)}"
    )


def format_digest(digest: Digest) -> str:
    """Render the full text of a digest summary message (Req 10.5, 10.6, 10.7).

    Produces a mode-specific header followed by one line per item. For the
    weekly-best-day digest the header additionally names the chosen UTC day so
    the recipient sees which day was picked as the best (Req 10.7).

    :param digest: The non-empty digest to render (its ``items`` always hold at
        least one entry, since :func:`build_digest` returns ``None`` for an
        empty period — Req 10.8).
    :returns: The fully formatted, multi-line digest text.
    """

    header = _HEADERS.get(digest.mode, "📋 Surf digest")
    lines: list[str] = [header]

    if digest.mode is NotificationMode.WEEKLY_BEST_DAY:
        # All items belong to the single chosen day; name it for context.
        best_day = _to_utc(digest.items[0].timestamp).date()
        lines.append(f"Best day: {best_day.isoformat()}")

    lines.extend(_format_item(item) for item in digest.items)
    return "\n".join(lines)
