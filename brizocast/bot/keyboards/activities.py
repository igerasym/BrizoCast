"""Activity-selection inline keyboard and its callback-data codec (pure).

Builds the onboarding/`/add` activity picker (Req 1.1) from the activity
registry's entries. Available activities (e.g. Surf) render as selectable
buttons; activities not yet supported in the MVP still render — marked with a
lock glyph (Req 1.3) — and carry callback data flagged unavailable so the
onboarding handler can tell the user the activity is not supported yet and keep
them on the selection step (Req 1.4).

The codec mirrors the feedback scheme's style under the distinct ``"act"``
namespace. All functions are pure: no service calls, no I/O.

Callback-data scheme
--------------------
::

    act:1:<a|u>:<activity_key>
    │   │ │     └─ activity key (free-form, last field)
    │   │ └─────── availability: 'a' available / 'u' unavailable
    │   └───────── scheme version
    └───────────── namespace prefix
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from brizocast.activities.base import Activity
from brizocast.bot.keyboards.callbacks import (
    NAMESPACE_ACTIVITY,
    encode_fields,
    split_fields,
)

__all__ = [
    "ActivitySelection",
    "build_activity_keyboard",
    "encode_activity_callback",
    "parse_activity_callback",
]

_AVAILABLE_TOKEN = "a"
_UNAVAILABLE_TOKEN = "u"

_ACTIVITY_FIELD_COUNT = 2  # availability token + activity key

# Glyph appended to activities that are registered but not selectable yet.
_UNAVAILABLE_MARKER = " 🔒"


@dataclass(frozen=True, slots=True)
class ActivitySelection:
    """Parsed identity of an activity-selection tap.

    ``available`` distinguishes a selectable activity from a not-yet-supported
    one, so the handler can branch between advancing the flow (Req 1.5) and
    re-prompting with an "unavailable" notice (Req 1.4).
    """

    activity_key: str
    available: bool


def encode_activity_callback(activity_key: str, *, available: bool) -> str:
    """Encode an activity choice into a ``callback_data`` string.

    :param activity_key: The activity's stable key (e.g. ``"surf"``).
    :param available: Whether the activity is selectable in the MVP.
    :returns: A ``callback_data`` string within Telegram's 64-byte limit.
    :raises ValueError: If ``activity_key`` is empty or the payload is too long.
    """

    if not activity_key:
        raise ValueError("activity callback requires a non-empty activity_key")
    token = _AVAILABLE_TOKEN if available else _UNAVAILABLE_TOKEN
    return encode_fields(NAMESPACE_ACTIVITY, (token, activity_key))


def parse_activity_callback(raw: str) -> ActivitySelection:
    """Parse activity ``callback_data`` back into an :class:`ActivitySelection`.

    Inverse of :func:`encode_activity_callback`.

    :param raw: The ``callback_data`` string received from Telegram.
    :returns: The decoded activity selection.
    :raises ValueError: If ``raw`` is not a well-formed current-version activity
        payload (wrong prefix/version, missing fields, unknown availability
        token, or empty activity key).
    """

    token, activity_key = split_fields(raw, NAMESPACE_ACTIVITY, _ACTIVITY_FIELD_COUNT)
    if token == _AVAILABLE_TOKEN:
        available = True
    elif token == _UNAVAILABLE_TOKEN:
        available = False
    else:
        raise ValueError(f"unknown activity availability token {token!r}: {raw!r}")
    if not activity_key:
        raise ValueError(f"activity callback has empty activity_key: {raw!r}")
    return ActivitySelection(activity_key=activity_key, available=available)


def build_activity_keyboard(
    activities: Sequence[Activity[Any]],
    *,
    columns: int = 1,
) -> InlineKeyboardMarkup:
    """Build the activity-selection keyboard from registry entries (Req 1.1, 1.3).

    Pass ``ActivityRegistry.all()`` to include every registered activity:
    available ones become plain selectable buttons, while unavailable ones get a
    lock marker on their label and unavailable-flagged callback data so a tap is
    still routable to the "not yet supported" response (Req 1.3, 1.4).

    :param activities: Registered activities to display, in order.
    :param columns: Maximum number of buttons per row (must be >= 1).
    :returns: The assembled :class:`telegram.InlineKeyboardMarkup`.
    :raises ValueError: If ``columns`` < 1 or ``activities`` is empty.
    """

    if columns < 1:
        raise ValueError(f"columns must be >= 1, got {columns}")
    if not activities:
        raise ValueError("build_activity_keyboard requires at least one activity")

    buttons: list[InlineKeyboardButton] = []
    for activity in activities:
        available = activity.available_in_mvp
        label = activity.display_name if available else f"{activity.display_name}{_UNAVAILABLE_MARKER}"
        buttons.append(
            InlineKeyboardButton(
                label,
                callback_data=encode_activity_callback(activity.key, available=available),
            )
        )

    rows = [buttons[i : i + columns] for i in range(0, len(buttons), columns)]
    return InlineKeyboardMarkup(rows)
