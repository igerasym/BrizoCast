"""Unit tests for the ``/settings`` inline keyboards and codec (task 7.6).

Covers :mod:`brizocast.bot.keyboards.settings`: the preferences menu and snooze
submenu render the expected buttons (Req 13.5, 13.6), and every button's
callback data round-trips through the ``"set"``-namespaced codec.
"""

from __future__ import annotations

import pytest

from brizocast.bot.keyboards.callbacks import callback_namespace
from brizocast.bot.keyboards.settings import (
    SNOOZE_DURATION_MINUTES,
    SettingsAction,
    build_settings_menu_keyboard,
    build_snooze_keyboard,
    encode_settings_callback,
    parse_settings_callback,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("action", list(SettingsAction))
def test_settings_callback_round_trips(action: SettingsAction) -> None:
    """Every action round-trips through the codec, keeping its argument."""
    raw = encode_settings_callback(action, "1")
    parsed = parse_settings_callback(raw)
    assert parsed.action is action
    assert parsed.arg == "1"
    assert callback_namespace(raw) == "set"


def test_parse_rejects_unknown_action() -> None:
    """An unknown action token is rejected."""
    with pytest.raises(ValueError):
        parse_settings_callback("set:1:bogus:-")


def test_settings_menu_has_all_categories() -> None:
    """The menu offers mode, quiet hours (set/clear), mute/unmute, and snooze."""
    keyboard = build_settings_menu_keyboard()
    actions = {
        parse_settings_callback(button.callback_data).action
        for row in keyboard.inline_keyboard
        for button in row
        if isinstance(button.callback_data, str)
    }
    assert actions == {
        SettingsAction.MODE,
        SettingsAction.QUIET,
        SettingsAction.QUIET_CLEAR,
        SettingsAction.MUTE,
        SettingsAction.SNOOZE_MENU,
    }


def test_snooze_keyboard_offers_durations_and_clear() -> None:
    """The snooze submenu offers each duration plus a clear (arg '0') option."""
    keyboard = build_snooze_keyboard()
    args = [
        parse_settings_callback(button.callback_data).arg
        for row in keyboard.inline_keyboard
        for button in row
        if isinstance(button.callback_data, str)
    ]
    for minutes in SNOOZE_DURATION_MINUTES:
        assert str(minutes) in args
    assert "0" in args  # the clear-snooze button


def test_snooze_keyboard_rejects_empty_durations() -> None:
    """An empty duration sequence is rejected."""
    with pytest.raises(ValueError):
        build_snooze_keyboard([])
