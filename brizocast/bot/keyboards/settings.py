"""Settings-menu inline keyboards and their callback-data codec (pure).

Builds the keyboards that drive the ``/settings`` flow (Req 13.5) once a user
has picked a subscription: the top-level preferences menu (choose what to edit)
and the snooze-duration submenu. Notification-mode editing reuses
:mod:`brizocast.bot.keyboards.notifications` and subscription selection reuses
:mod:`brizocast.bot.keyboards.subscriptions`; this module owns only the
settings-specific actions (mute/unmute, quiet-hours entry/clear, and snooze).

The codec uses the distinct ``"set"`` namespace and a fixed two-field layout —
an :class:`SettingsAction` token plus a free-form ``arg`` — so a tap round-trips
to a :class:`SettingsCallback` the handler can dispatch on.

All functions are pure: no service calls, no I/O.

Callback-data scheme
--------------------
::

    set:1:<action>:<arg>
    │   │ │        └─ action argument ('-' when unused; minutes for snooze;
    │   │ │           '1'/'0' for mute on/off)
    │   │ └────────── SettingsAction value
    │   └──────────── scheme version
    └──────────────── namespace prefix
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from brizocast.bot.keyboards.callbacks import (
    NAMESPACE_SETTINGS,
    encode_fields,
    split_fields,
)

__all__ = [
    "SNOOZE_DURATION_MINUTES",
    "SettingsAction",
    "SettingsCallback",
    "build_settings_menu_keyboard",
    "build_snooze_keyboard",
    "encode_settings_callback",
    "parse_settings_callback",
]

_SETTINGS_FIELD_COUNT = 2  # action token + argument

# Placeholder argument for actions that carry no payload.
_NO_ARG = "-"

# Offered snooze durations, in minutes, for the snooze submenu.
SNOOZE_DURATION_MINUTES: tuple[int, ...] = (60, 180, 720, 1440)


class SettingsAction(StrEnum):
    """A settings-menu action, identifying which preference a tap edits."""

    MODE = "mode"  # open the notification-mode submenu (Req 10.2)
    QUIET = "quiet"  # begin quiet-hours text entry (Req 11.1)
    QUIET_CLEAR = "qhx"  # clear quiet hours (Req 11.1)
    MUTE = "mute"  # set mute state; arg '1' mutes, '0' unmutes (Req 11.3)
    SNOOZE_MENU = "snzm"  # open the snooze-duration submenu (Req 11.4)
    SNOOZE = "snz"  # set snooze; arg is minutes, '0' clears (Req 11.4)


@dataclass(frozen=True, slots=True)
class SettingsCallback:
    """Parsed identity of a settings-menu tap."""

    action: SettingsAction
    arg: str


def encode_settings_callback(action: SettingsAction, arg: str = _NO_ARG) -> str:
    """Encode a settings action into a ``callback_data`` string.

    :param action: The settings action the button performs.
    :param arg: The action argument; defaults to a placeholder for actions that
        carry no payload.
    :returns: A ``callback_data`` string within Telegram's 64-byte limit.
    """

    return encode_fields(NAMESPACE_SETTINGS, (action.value, arg))


def parse_settings_callback(raw: str) -> SettingsCallback:
    """Parse settings ``callback_data`` back into a :class:`SettingsCallback`.

    Inverse of :func:`encode_settings_callback`.

    :param raw: The ``callback_data`` string received from Telegram.
    :returns: The decoded settings callback.
    :raises ValueError: If ``raw`` is malformed or carries an unknown action.
    """

    action_token, arg = split_fields(raw, NAMESPACE_SETTINGS, _SETTINGS_FIELD_COUNT)
    try:
        action = SettingsAction(action_token)
    except ValueError as exc:
        raise ValueError(f"unknown settings action {action_token!r}: {raw!r}") from exc
    return SettingsCallback(action=action, arg=arg)


def build_settings_menu_keyboard() -> InlineKeyboardMarkup:
    """Build the top-level settings preferences menu (Req 13.5, 13.6).

    Offers the editable notification preferences: notification mode, quiet hours
    (set or clear), mute/unmute, and snooze.

    :returns: The assembled :class:`telegram.InlineKeyboardMarkup`.
    """

    rows = [
        [
            InlineKeyboardButton(
                "⚡ Notification mode",
                callback_data=encode_settings_callback(SettingsAction.MODE),
            )
        ],
        [
            InlineKeyboardButton(
                "🌙 Set quiet hours",
                callback_data=encode_settings_callback(SettingsAction.QUIET),
            ),
            InlineKeyboardButton(
                "✖️ Clear quiet hours",
                callback_data=encode_settings_callback(SettingsAction.QUIET_CLEAR),
            ),
        ],
        [
            InlineKeyboardButton(
                "🔕 Mute",
                callback_data=encode_settings_callback(SettingsAction.MUTE, "1"),
            ),
            InlineKeyboardButton(
                "🔔 Unmute",
                callback_data=encode_settings_callback(SettingsAction.MUTE, "0"),
            ),
        ],
        [
            InlineKeyboardButton(
                "💤 Snooze",
                callback_data=encode_settings_callback(SettingsAction.SNOOZE_MENU),
            )
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _format_duration(minutes: int) -> str:
    """Render a snooze duration in whole hours (e.g. 180 → '3h')."""

    hours = minutes // 60
    return f"{hours}h"


def build_snooze_keyboard(
    durations_minutes: Sequence[int] = SNOOZE_DURATION_MINUTES,
) -> InlineKeyboardMarkup:
    """Build the snooze-duration submenu (Req 11.4, 13.6).

    Renders one button per offered duration plus a button that clears any
    active snooze.

    :param durations_minutes: The snooze durations to offer, in minutes.
    :returns: The assembled :class:`telegram.InlineKeyboardMarkup`.
    :raises ValueError: If ``durations_minutes`` is empty.
    """

    if not durations_minutes:
        raise ValueError("build_snooze_keyboard requires at least one duration")

    duration_buttons = [
        InlineKeyboardButton(
            _format_duration(minutes),
            callback_data=encode_settings_callback(SettingsAction.SNOOZE, str(minutes)),
        )
        for minutes in durations_minutes
    ]
    clear_button = InlineKeyboardButton(
        "✖️ Clear snooze",
        callback_data=encode_settings_callback(SettingsAction.SNOOZE, "0"),
    )
    return InlineKeyboardMarkup([duration_buttons, [clear_button]])
