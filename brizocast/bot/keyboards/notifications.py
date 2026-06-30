"""Notification-mode inline keyboard and its callback-data codec (pure).

Builds the keyboard that lets a user choose a subscription's
:class:`~brizocast.notifications.modes.NotificationMode` (Req 10.1, 10.2) within
the ``/settings`` flow (Req 13.5). The codec uses the distinct ``"nm"``
namespace and carries the mode's stable string key, so a tap round-trips
straight to a :class:`NotificationMode` the settings handler can persist.

All functions are pure: no service calls, no I/O.

Callback-data scheme
--------------------
::

    nm:1:<mode_key>
    │  │ └─ NotificationMode value (e.g. 'immediate', 'morning_digest')
    │  └─── scheme version
    └────── namespace prefix
"""

from __future__ import annotations

from collections.abc import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from brizocast.bot.keyboards.callbacks import (
    NAMESPACE_NOTIFICATION_MODE,
    encode_fields,
    split_fields,
)
from brizocast.notifications.modes import NotificationMode

__all__ = [
    "build_notification_mode_keyboard",
    "encode_notification_mode_callback",
    "notification_mode_label",
    "parse_notification_mode_callback",
]

_MODE_FIELD_COUNT = 1  # mode key

# User-facing labels per mode.
_MODE_LABELS: dict[NotificationMode, str] = {
    NotificationMode.IMMEDIATE: "⚡ Immediate alerts",
    NotificationMode.MORNING_DIGEST: "🌅 Morning digest",
    NotificationMode.EVENING_DIGEST: "🌇 Evening digest",
    NotificationMode.WEEKLY_BEST_DAY: "📅 Weekly best day",
}


def notification_mode_label(mode: NotificationMode) -> str:
    """Return the user-facing label for a notification mode.

    Falls back to the mode's title-cased key if no curated label exists, so the
    function stays total even if a mode is added without a label entry.
    """

    return _MODE_LABELS.get(mode, mode.value.replace("_", " ").title())


def encode_notification_mode_callback(mode: NotificationMode) -> str:
    """Encode a notification mode into a ``callback_data`` string.

    :param mode: The chosen :class:`NotificationMode`.
    :returns: A ``callback_data`` string within Telegram's 64-byte limit.
    """

    return encode_fields(NAMESPACE_NOTIFICATION_MODE, (mode.value,))


def parse_notification_mode_callback(raw: str) -> NotificationMode:
    """Parse mode ``callback_data`` back into a :class:`NotificationMode`.

    Inverse of :func:`encode_notification_mode_callback`.

    :param raw: The ``callback_data`` string received from Telegram.
    :returns: The decoded :class:`NotificationMode`.
    :raises ValueError: If ``raw`` is malformed or carries an unknown mode key.
    """

    (token,) = split_fields(raw, NAMESPACE_NOTIFICATION_MODE, _MODE_FIELD_COUNT)
    try:
        return NotificationMode.from_key(token)
    except ValueError as exc:
        raise ValueError(f"unknown notification mode {token!r}: {raw!r}") from exc


def build_notification_mode_keyboard(
    modes: Sequence[NotificationMode] = tuple(NotificationMode),
) -> InlineKeyboardMarkup:
    """Build the notification-mode keyboard (Req 10.1, 10.2, 13.5, 13.6).

    Renders one button per mode, one per row, in the given order (defaulting to
    every mode). Each button carries its mode's encoded key.

    :param modes: The modes to offer; defaults to all of them. Restrict this to
        gate modes by plan entitlement (Req 21) at the call site.
    :returns: The assembled :class:`telegram.InlineKeyboardMarkup`.
    :raises ValueError: If ``modes`` is empty.
    """

    if not modes:
        raise ValueError("build_notification_mode_keyboard requires at least one mode")

    rows = [
        [
            InlineKeyboardButton(
                notification_mode_label(mode),
                callback_data=encode_notification_mode_callback(mode),
            )
        ]
        for mode in modes
    ]
    return InlineKeyboardMarkup(rows)
