"""`/subscriptions` list renderer (pure function, no I/O).

Renders the user's subscriptions for the ``/subscriptions`` command (Req 3.5):
one line per subscription showing its activity, location, search radius, and
notification mode. Consumes the presentation-ready
:class:`~brizocast.services.subscription_service.SubscriptionSummary` values
produced by ``SubscriptionService.summarize_for_user`` so the formatter never
touches ORM rows or opens a session.

Pure and presentation-only: no service calls, no persistence.

Requirements covered: 3.5.
"""

from __future__ import annotations

from collections.abc import Sequence

from brizocast.notifications.modes import NotificationMode
from brizocast.services.subscription_service import SubscriptionSummary

__all__ = ["format_subscriptions_list"]

_HEADER = "📋 Your subscriptions"
_EMPTY = "You have no subscriptions yet. Use /add to create one."

# Friendly labels for notification modes, reused from the keyboard layer's
# vocabulary but kept self-contained so this stays a pure formatter.
_MODE_LABELS: dict[str, str] = {
    NotificationMode.IMMEDIATE.value: "Immediate",
    NotificationMode.MORNING_DIGEST.value: "Morning digest",
    NotificationMode.EVENING_DIGEST.value: "Evening digest",
    NotificationMode.WEEKLY_BEST_DAY.value: "Weekly best day",
}


def _mode_label(mode_key: str) -> str:
    """Render a persisted notification-mode key as a friendly label.

    Falls back to a title-cased form of the raw key so an unrecognised mode
    still renders rather than raising.
    """

    return _MODE_LABELS.get(mode_key, mode_key.replace("_", " ").title())


def _format_line(index: int, summary: SubscriptionSummary) -> str:
    """Render one subscription as a numbered line (Req 3.5).

    Includes the activity, location, search radius (km), and notification mode.
    """

    return (
        f"{index}. {summary.activity_display_name}"
        f" · {summary.location_place}"
        f" · {summary.search_radius_km:g} km"
        f" · {_mode_label(summary.notification_mode)}"
    )


def format_subscriptions_list(summaries: Sequence[SubscriptionSummary]) -> str:
    """Render the ``/subscriptions`` list text (Req 3.5).

    Produces a header followed by one numbered line per subscription, each
    naming the activity, location, search radius, and notification mode. When
    the user has no subscriptions, returns a friendly empty-state message
    pointing at ``/add``.

    :param summaries: The user's subscription summaries, in display order.
    :returns: The fully formatted, multi-line list text.
    """

    if not summaries:
        return _EMPTY

    lines = [_HEADER]
    lines.extend(_format_line(index, summary) for index, summary in enumerate(summaries, start=1))
    return "\n".join(lines)
