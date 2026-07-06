"""Command and menu text renderers (pure functions, no I/O).

Presentation helpers for the bot's command surface and conversational scaffolding:

* :func:`format_help` — lists every supported command with a description
  (Req 13.2), and :data:`COMMANDS` is the single source of truth for the command
  set (Req 13.1) so help text and command registration (task 7.9) cannot drift.
* :func:`format_status` — reports the active-subscription count and the most
  recent scheduler-run time (Req 13.3).
* :func:`format_forecast_result` / :func:`format_forecast_no_spots` — report the
  best surf score and spot for a subscription, or a no-nearby-spots notice
  (Req 13.4, Req 5.5).
* :func:`format_presets_list` — lists the available default and custom presets
  (Req 4.3).
* :func:`onboarding_welcome_text`, :func:`main_menu_text`,
  :func:`activity_unavailable_text`, :func:`unknown_command_text` — onboarding,
  main-menu, and fallback copy (Req 1.1, 1.4, 13.7).

All functions are pure: no service calls, no persistence, no time lookups
(the caller supplies any "now"/timestamp values).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from brizocast.services.preset_service import PresetOption, PresetSource

__all__ = [
    "COMMANDS",
    "activity_unavailable_text",
    "format_forecast_no_spots",
    "format_forecast_result",
    "format_help",
    "format_presets_list",
    "format_status",
    "main_menu_text",
    "onboarding_location_prompt_text",
    "onboarding_welcome_text",
    "unknown_command_text",
]

# Single source of truth for the bot's command set (Req 13.1). Order is the
# order shown in /help and used for command registration.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("/start", "Begin onboarding or open the main menu"),
    ("/location", "Set or manage your locations"),
    ("/subscriptions", "List your subscriptions"),
    ("/add", "Create a new subscription"),
    ("/remove", "Delete a subscription"),
    ("/settings", "Edit notification preferences"),
    ("/presets", "Browse condition presets"),
    ("/status", "Show active subscriptions and last check time"),
    ("/forecast", "Get the current best score for a subscription"),
    ("/help", "Show this list of commands"),
)

_HELP_HEADER = "🤖 BrizoCast commands"
_STATUS_HEADER = "📊 Status"
_PRESETS_HEADER = "🎛️ Presets"
_PRESETS_EMPTY = "No presets available."
_NO_SCHEDULER_RUN = "never"

# Labels for the source of a listed preset (Req 4.3).
_SOURCE_LABELS: dict[PresetSource, str] = {
    PresetSource.STATIC_DEFAULT: "default",
    PresetSource.PERSISTED_DEFAULT: "default",
    PresetSource.USER_CUSTOM: "custom",
}

_AI_MARKER = " ✨"


def _to_utc(value: datetime) -> datetime:
    """Normalize a datetime to UTC, treating naive values as already-UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def format_help() -> str:
    """Render the ``/help`` text listing every command and its description (Req 13.2).

    :returns: The fully formatted, multi-line help text built from
        :data:`COMMANDS`.
    """

    lines = [_HELP_HEADER]
    lines.extend(f"{command} — {description}" for command, description in COMMANDS)
    return "\n".join(lines)


def format_status(active_subscription_count: int, last_scheduler_run: datetime | None) -> str:
    """Render the ``/status`` text (Req 13.3).

    Reports how many subscriptions are active and when the scheduler last ran.

    :param active_subscription_count: Number of active subscriptions.
    :param last_scheduler_run: Timestamp of the most recent scheduler run, or
        ``None`` if it has not run yet.
    :returns: The fully formatted, multi-line status text.
    """

    if last_scheduler_run is None:
        last_run_text = _NO_SCHEDULER_RUN
    else:
        last_run_text = _to_utc(last_scheduler_run).strftime("%Y-%m-%d %H:%M UTC")

    return "\n".join(
        (
            _STATUS_HEADER,
            f"Active subscriptions: {active_subscription_count}",
            f"Last check: {last_run_text}",
        )
    )


def format_forecast_result(
    *,
    location_label: str,
    spot_name: str,
    score: int,
    category_label: str,
) -> str:
    """Render the ``/forecast`` best-result text for a subscription (Req 13.4).

    Reports the current best surf score, its category, and the spot that earned
    it. The caller resolves the best score regardless of the subscription's
    mute/snooze state (Req 13.4) and supplies the values here.

    :param location_label: The subscription's location label, for context.
    :param spot_name: The surf spot with the best current score.
    :param score: The best current surf score (0..100).
    :param category_label: The human-readable score category (e.g. ``"Good"``).
    :returns: The fully formatted, multi-line forecast text.
    """

    return "\n".join(
        (
            f"🔮 Best right now near {location_label}",
            f"🏄 {spot_name}",
            f"Score {score} ({category_label})",
        )
    )


def format_forecast_no_spots(location_label: str) -> str:
    """Render the ``/forecast`` no-nearby-spots notice (Req 13.4, Req 5.5).

    :param location_label: The subscription's location label, for context.
    :returns: A short message explaining no spots are within the search radius.
    """

    return f"🔮 No surf spots found within range of {location_label}."


def _preset_line(option: PresetOption) -> str:
    """Render one preset as a list line: name, region, source, AI marker."""

    label = option.name
    if option.region:
        label = f"{label} ({option.region})"
    source = _SOURCE_LABELS.get(option.source, option.source.value)
    line = f"• {label} — {source}"
    if option.ai_generated:
        line = f"{line}{_AI_MARKER}"
    return line


def format_presets_list(options: Sequence[PresetOption]) -> str:
    """Render the ``/presets`` list text (Req 4.3).

    Lists the available default and custom presets, one per line, noting each
    one's source and marking AI-generated defaults.

    :param options: The presets to list, in listing order.
    :returns: The fully formatted, multi-line presets text (or an empty-state
        message when there are none).
    """

    if not options:
        return _PRESETS_EMPTY

    lines = [_PRESETS_HEADER]
    lines.extend(_preset_line(option) for option in options)
    return "\n".join(lines)


def onboarding_welcome_text() -> str:
    """Render the onboarding welcome prompting an activity choice (Req 1.1)."""

    return (
        "👋 Welcome to BrizoCast!\n"
        "Let's get you set up. Which activity do you want condition alerts for?"
    )


def main_menu_text() -> str:
    """Render the main-menu text shown to already-onboarded users (Req 1.6)."""

    return (
        "🏄 *BrizoCast* — surf alert bot\n\n"
        "I monitor wave forecasts and notify you when conditions are good "
        "at your spots.\n\n"
        "📋 *My subscriptions* — view, add, or manage your alerts\n\n"
        "Tap the button below to get started 👇"
    )


def onboarding_location_prompt_text(activity_display_name: str) -> str:
    """Render the advance-to-location-setup prompt after Surf is chosen (Req 1.5).

    Confirms the selected activity and directs the user to ``/location`` to set
    up where they want condition alerts — the handoff into the location-setup
    step (task 7.3).

    :param activity_display_name: The selected activity's display name.
    :returns: A message confirming the activity and prompting ``/location``.
    """

    return (
        f"Great — {activity_display_name} it is! 🌊\n"
        "Next, let's set your location. Send /location to choose where you "
        "want condition alerts."
    )


def activity_unavailable_text(activity_display_name: str) -> str:
    """Render the not-yet-supported notice for an unavailable activity (Req 1.4).

    :param activity_display_name: The selected activity's display name.
    :returns: A message telling the user the activity is not supported yet and
        to pick another.
    """

    return (
        f"{activity_display_name} isn't supported yet — it's coming soon. "
        "Please pick an available activity."
    )


def unknown_command_text() -> str:
    """Render the unrecognised-command fallback suggesting ``/help`` (Req 13.7)."""

    return "Sorry, I didn't recognise that command. Try /help to see what I can do."
