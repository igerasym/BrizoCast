"""Unit tests for the subscriptions list and command/menu formatters (task 7.1).

Covers the pure presentation renderers in
:mod:`brizocast.bot.formatters.subscriptions` and
:mod:`brizocast.bot.formatters.commands`:

* the ``/subscriptions`` formatter renders activity, location, radius, and mode
  per subscription (Req 3.5);
* ``/help`` lists every command (Req 13.2), ``/status`` reports count and last
  run (Req 13.3), and ``/forecast`` reports best score and spot (Req 13.4);
* ``/presets`` lists default and custom presets (Req 4.3).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brizocast.bot.formatters.commands import (
    COMMANDS,
    format_forecast_no_spots,
    format_forecast_result,
    format_help,
    format_presets_list,
    format_status,
    unknown_command_text,
)
from brizocast.bot.formatters.subscriptions import format_subscriptions_list
from brizocast.notifications.modes import NotificationMode
from brizocast.services.preset_service import PresetOption, PresetSource
from brizocast.services.subscription_service import SubscriptionSummary

pytestmark = pytest.mark.unit

# Commands required by Req 13.1.
_REQUIRED_COMMANDS = {
    "/start",
    "/location",
    "/subscriptions",
    "/add",
    "/remove",
    "/settings",
    "/presets",
    "/status",
    "/forecast",
    "/help",
}


def _summary(**overrides: object) -> SubscriptionSummary:
    base: dict[str, object] = {
        "subscription_id": 1,
        "activity_key": "surf",
        "activity_display_name": "🏄 Surf",
        "location_label": "Ericeira",
        "location_place": "Ericeira, Portugal",
        "search_radius_km": 30.0,
        "notification_mode": NotificationMode.MORNING_DIGEST.value,
    }
    base.update(overrides)
    return SubscriptionSummary(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# /subscriptions (Req 3.5)
# --------------------------------------------------------------------------- #
def test_subscriptions_list_renders_each_field_per_sub() -> None:
    text = format_subscriptions_list([_summary()])
    assert "🏄 Surf" in text  # activity
    assert "Ericeira, Portugal" in text  # location
    assert "30 km" in text  # radius
    assert "Morning digest" in text  # mode


def test_subscriptions_list_numbers_each_subscription() -> None:
    text = format_subscriptions_list(
        [_summary(subscription_id=1), _summary(subscription_id=2, location_place="Peniche, Portugal")]
    )
    assert "1. " in text
    assert "2. " in text
    assert "Peniche, Portugal" in text


def test_subscriptions_list_empty_state() -> None:
    text = format_subscriptions_list([])
    assert "/add" in text


# --------------------------------------------------------------------------- #
# /help (Req 13.1, 13.2)
# --------------------------------------------------------------------------- #
def test_help_lists_every_required_command_with_description() -> None:
    text = format_help()
    listed = {command for command, _ in COMMANDS}
    assert _REQUIRED_COMMANDS <= listed
    for command, description in COMMANDS:
        assert command in text
        assert description in text


# --------------------------------------------------------------------------- #
# /status (Req 13.3)
# --------------------------------------------------------------------------- #
def test_status_reports_count_and_last_run() -> None:
    text = format_status(3, datetime(2025, 6, 21, 6, 30, tzinfo=UTC))
    assert "3" in text
    assert "2025-06-21 06:30 UTC" in text


def test_status_handles_no_scheduler_run() -> None:
    text = format_status(0, None)
    assert "0" in text
    assert "never" in text


# --------------------------------------------------------------------------- #
# /forecast (Req 13.4)
# --------------------------------------------------------------------------- #
def test_forecast_result_reports_best_score_and_spot() -> None:
    text = format_forecast_result(
        location_label="Ericeira",
        spot_name="Ribeira d'Ilhas",
        score=88,
        category_label="Excellent",
    )
    assert "Ribeira d'Ilhas" in text
    assert "88" in text
    assert "Excellent" in text


def test_forecast_no_spots_message() -> None:
    text = format_forecast_no_spots("Ericeira")
    assert "Ericeira" in text


# --------------------------------------------------------------------------- #
# /presets (Req 4.3)
# --------------------------------------------------------------------------- #
def test_presets_list_renders_default_and_custom() -> None:
    options = [
        PresetOption(
            name="Beach break",
            region="Portugal",
            params=object(),  # type: ignore[arg-type]
            source=PresetSource.STATIC_DEFAULT,
            preset_id=None,
            ai_generated=False,
        ),
        PresetOption(
            name="My spot",
            region=None,
            params=object(),  # type: ignore[arg-type]
            source=PresetSource.USER_CUSTOM,
            preset_id=5,
            ai_generated=False,
        ),
    ]
    text = format_presets_list(options)
    assert "Beach break" in text
    assert "default" in text
    assert "My spot" in text
    assert "custom" in text


def test_presets_list_empty_state() -> None:
    assert format_presets_list([]) == "No presets available."


def test_unknown_command_suggests_help() -> None:
    assert "/help" in unknown_command_text()
