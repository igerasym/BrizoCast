"""Persistent main-menu reply keyboard and command-menu definitions.

Gives the bot a always-visible bottom navigation keyboard so a user can move
through the main sections by tapping, rather than remembering slash commands.
Each button's label is matched by :func:`menu_filter` and wired as an extra
*entry point* into the corresponding command/conversation handler, so tapping a
button does exactly what typing the command would (Req 13.1, 13.2 — discovery).

The same section list is exposed as :data:`BOT_COMMANDS` for
``Bot.set_my_commands`` so the native Telegram "≡ Menu" button lists every
command with a description too.
"""

from __future__ import annotations

import re
from typing import Final

from telegram import BotCommand, ReplyKeyboardMarkup
from telegram.ext import filters

__all__ = [
    "BOT_COMMANDS",
    "MENU_LABELS",
    "MENU_LABEL_ADD",
    "MENU_LABEL_SUBSCRIPTIONS",
    "MENU_PROMPT",
    "any_menu_label_filter",
    "build_main_menu_keyboard",
    "menu_filter",
]

MENU_LABEL_ADD: Final = "➕ Add subscription"
MENU_LABEL_SUBSCRIPTIONS: Final = "📋 My subscriptions"

MENU_PROMPT: Final = "Tap below to get started."

_LAYOUT: Final[list[list[str]]] = [
    [MENU_LABEL_SUBSCRIPTIONS],
]

MENU_LABELS: Final[list[str]] = [
    MENU_LABEL_SUBSCRIPTIONS,
]

BOT_COMMANDS: Final[list[BotCommand]] = [
    BotCommand("start", "Start the bot / show main menu"),
    BotCommand("subscriptions", "My subscriptions"),
]


def build_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Build the persistent bottom navigation keyboard.

    ``is_persistent`` keeps it visible, ``resize_keyboard`` makes the buttons
    compact, and the input-field placeholder hints at tapping a section.
    """
    return ReplyKeyboardMarkup(
        _LAYOUT,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Pick a section…",
    )


def menu_filter(label: str) -> filters.BaseFilter:
    """Return a message filter matching exactly the menu-button ``label``.

    Used as the filter of a :class:`~telegram.ext.MessageHandler` entry point so
    a tapped button enters the same flow its slash command would, but only when
    the whole message equals the label (never as free-text inside a flow).
    """
    return filters.Regex(rf"^{re.escape(label)}$")


def any_menu_label_filter() -> filters.BaseFilter:
    """Return a filter matching any whole-message menu label.

    Useful as a negative guard (``& ~any_menu_label_filter()``) so a free-text
    handler inside a conversation never mistakes a tapped menu button for input.
    """
    pattern = "|".join(re.escape(label) for label in MENU_LABELS)
    return filters.Regex(rf"^(?:{pattern})$")
