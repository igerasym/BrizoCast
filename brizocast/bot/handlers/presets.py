"""``/presets`` handler and the custom-conditions conversation (Req 4.1, 4.3, 4.5, 4.8).

Two thin Telegram adapters, assembled and returned by
:func:`build_preset_handlers`:

* The ``/presets`` command lists the available default presets plus the user's
  own custom presets (Req 4.1, 4.3) via
  :meth:`~brizocast.services.preset_service.PresetService.list_presets`,
  rendering the text with the pure
  :func:`~brizocast.bot.formatters.commands.format_presets_list` helper and
  offering the preset-pick inline keyboard
  (:func:`~brizocast.bot.keyboards.presets.build_preset_pick_keyboard`).
* The **custom-conditions conversation** (a
  :class:`~telegram.ext.ConversationHandler`) walks the user through every
  Custom_Conditions field (Req 4.5): minimum/maximum wave height, minimum swell
  period, maximum wind, acceptable wind/swell direction, an optional tide
  preference, and a daylight-only flag. The collected primitive fields are
  assembled into a :class:`~brizocast.activities.surf.conditions.SurfConditions`
  via
  :func:`~brizocast.services.preset_service.surf_conditions_from_fields`, whose
  validation rejects an inverted wave band (minimum greater than maximum,
  Req 4.8) and other domain violations by raising
  :class:`~brizocast.core.errors.DomainValidationError`. The conversation also
  guards the wave band *inline* at the maximum-wave step so the offending value
  is re-requested immediately. On success the conditions are persisted with
  :meth:`~brizocast.services.preset_service.PresetService.create_custom_conditions`.

Thin handler + dependency injection
-----------------------------------
:func:`build_preset_handlers` is a *builder closure*: it receives the live
:class:`~brizocast.services.preset_service.PresetService` and binds it into the
handler callbacks, returning the assembled handlers for the composition root
(task 11.1) to register on the ``Application``. Handlers only parse the update,
call the service, and render a reply — no persistence, scoring, or provider
logic lives here. This module never wires the ``Application`` itself.

Handoff contract (``context.user_data``)
-----------------------------------------
Because only the :class:`PresetService` is injected, the two pieces of
conversation context the handlers cannot derive from a Telegram update are read
from ``context.user_data`` under documented, module-level keys, written by the
flow that launches each interaction:

* :data:`SUBSCRIPTION_ID_KEY` — the database id of the subscription the custom
  conditions belong to. This is the very key the ``/add`` flow (task 7.4) writes
  (``CTX_PENDING_CUSTOM_SUBSCRIPTION_ID``) when the user picks custom conditions,
  re-exported here so this conversation reads exactly what the subscriptions
  handler hands off; a ``/settings`` entry (task 7.6) may also set it. If it is
  absent when the conversation is entered, the handler tells the user to start
  from ``/add`` or ``/settings`` and ends without persisting anything.
* :data:`USER_ID_KEY` — the database id of the requesting user
  (``CTX_DB_USER_ID``, resolved by the onboarding/user-provisioning layer,
  task 7.2). Required by ``/presets`` to list that user's own custom presets. If
  absent, ``/presets`` asks the user to run ``/start`` first.
* :data:`REGION_KEY` — optional. The region used to scope the default presets
  shown by ``/presets``; when absent, all bundled defaults are listed.

The conversation stores its in-progress field values under
:data:`_DRAFT_KEY` and clears them (and :data:`SUBSCRIPTION_ID_KEY`) when it
ends.
"""

from __future__ import annotations

import math
from enum import IntEnum
from typing import Any

from telegram import Update
from telegram.ext import (
    BaseHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from brizocast.activities.surf.conditions import TidePreference
from brizocast.activities.surf.directions import compass_to_degrees
from brizocast.bot.formatters.commands import format_presets_list
from brizocast.bot.handlers.subscriptions import (
    CTX_DB_USER_ID,
    CTX_PENDING_CUSTOM_SUBSCRIPTION_ID,
)
from brizocast.bot.keyboards.presets import build_preset_pick_keyboard
from brizocast.core.errors import DomainValidationError
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.services.preset_service import (
    PresetService,
    surf_conditions_from_fields,
)

__all__ = [
    "REGION_KEY",
    "SUBSCRIPTION_ID_KEY",
    "USER_ID_KEY",
    "CustomConditionsState",
    "build_preset_handlers",
]

# -- handoff contract keys in context.user_data ------------------------- #

#: Database id of the subscription the custom conditions are for. This is the
#: same key the ``/add`` flow (task 7.4) writes when the user chooses custom
#: conditions, re-exported here so the conversation reads exactly what the
#: subscriptions handler hands off. A ``/settings`` entry (task 7.6) may set it
#: too before launching the conversation.
SUBSCRIPTION_ID_KEY = CTX_PENDING_CUSTOM_SUBSCRIPTION_ID
#: Database id of the requesting user (set by the onboarding layer, task 7.2).
USER_ID_KEY = CTX_DB_USER_ID
#: Optional region scoping the default presets shown by ``/presets``.
REGION_KEY = "active_region"

# Key under which the in-progress custom-conditions field values are stashed.
_DRAFT_KEY = "custom_conditions_draft"

# Words that mean "no value / skip this optional field".
_SKIP_WORDS = frozenset({"skip", "none", "any", "-"})
_TRUE_WORDS = frozenset({"yes", "y", "true", "1", "on"})
_FALSE_WORDS = frozenset({"no", "n", "false", "0", "off"})

# Inclusive bounds for a direction entered as raw degrees.
_DIRECTION_MIN_DEG = 0.0
_DIRECTION_MAX_DEG = 360.0

# -- conversation copy --------------------------------------------------- #

_NO_SUBSCRIPTION_TEXT = (
    "I don't know which subscription these conditions are for. "
    "Start from /add to create one, or pick a subscription in /settings, "
    "then set custom conditions."
)
_NO_USER_TEXT = "Please run /start first so I can find your presets."
_CANCELLED_TEXT = "Custom conditions cancelled. Nothing was changed."
_INTRO_TEXT = (
    "🎛️ Let's set your custom surf conditions. "
    "Send each value as I ask for it, or /cancel to stop."
)
_PROMPT_MIN_WAVE = "1/8 — Minimum wave height in metres? (e.g. 0.8)"
_PROMPT_MAX_WAVE = "2/8 — Maximum wave height in metres? (e.g. 2.5)"
_PROMPT_MIN_PERIOD = "3/8 — Minimum swell period in seconds? (e.g. 9)"
_PROMPT_MAX_WIND = "4/8 — Maximum wind in km/h? (e.g. 25)"
_PROMPT_WIND_DIR = (
    "5/8 — Acceptable wind direction? A compass point (e.g. NW), degrees "
    "(0-360), or 'skip' for any."
)
_PROMPT_SWELL_DIR = (
    "6/8 — Acceptable swell direction? A compass point (e.g. W), degrees "
    "(0-360), or 'skip' for any."
)
_PROMPT_TIDE = "7/8 — Preferred tide? low, mid, high, or 'skip' for no preference."
_PROMPT_DAYLIGHT = "8/8 — Daylight hours only? yes or no."

_INVALID_NUMBER = "That doesn't look like a non-negative number. Please try again."
_INVALID_WAVE_BAND = (
    "Maximum wave height must be greater than or equal to the minimum "
    "({min_wave:g} m). Please enter a maximum of at least {min_wave:g} m."
)
_INVALID_DIRECTION = (
    "Please send a compass point (e.g. NW), a number of degrees between 0 and "
    "360, or 'skip'."
)
_INVALID_TIDE = "Please send one of: low, mid, high, or 'skip'."
_INVALID_BOOL = "Please answer 'yes' or 'no'."
_SAVED_TEXT = "✅ Saved your custom conditions for this subscription."


class CustomConditionsState(IntEnum):
    """States of the custom-conditions conversation, one per collected field."""

    MIN_WAVE = 0
    MAX_WAVE = 1
    MIN_PERIOD = 2
    MAX_WIND = 3
    WIND_DIR = 4
    SWELL_DIR = 5
    TIDE = 6
    DAYLIGHT = 7


# -- pure input parsing helpers ----------------------------------------- #


def _parse_nonneg_float(text: str) -> float | None:
    """Parse ``text`` into a finite, non-negative float, or ``None`` if invalid."""

    try:
        value = float(text.strip())
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0.0:
        return None
    return value


def _parse_direction(text: str) -> tuple[bool, float | None]:
    """Parse a direction: a compass point, degrees, or a skip word.

    :returns: ``(ok, value)`` where ``ok`` is whether the input was understood
        and ``value`` is the bearing in degrees (``None`` for a skipped/any
        direction). When ``ok`` is ``False`` the input should be re-requested.
    """

    cleaned = text.strip()
    if cleaned.lower() in _SKIP_WORDS:
        return True, None
    try:
        return True, compass_to_degrees(cleaned)
    except DomainValidationError:
        pass
    try:
        degrees = float(cleaned)
    except ValueError:
        return False, None
    if math.isfinite(degrees) and _DIRECTION_MIN_DEG <= degrees <= _DIRECTION_MAX_DEG:
        return True, degrees
    return False, None


def _parse_tide(text: str) -> tuple[bool, TidePreference | None]:
    """Parse a tide preference or a skip word.

    :returns: ``(ok, value)`` mirroring :func:`_parse_direction`.
    """

    cleaned = text.strip().lower()
    if cleaned in _SKIP_WORDS:
        return True, None
    try:
        return True, TidePreference(cleaned)
    except ValueError:
        return False, None


def _parse_bool(text: str) -> bool | None:
    """Parse a yes/no answer, or ``None`` if it is neither."""

    cleaned = text.strip().lower()
    if cleaned in _TRUE_WORDS:
        return True
    if cleaned in _FALSE_WORDS:
        return False
    return None


def build_preset_handlers(
    preset_service: PresetService,
    *,
    logger: BoundLogger | None = None,
) -> list[BaseHandler[Update, ContextTypes.DEFAULT_TYPE, Any]]:
    """Build the ``/presets`` command and custom-conditions conversation handlers.

    :param preset_service: The application's preset/conditions service, injected
        into the handler callbacks via this closure.
    :param logger: Optional bound logger; one is created when omitted.
    :returns: The handlers to register on the ``Application`` (a ``/presets``
        :class:`~telegram.ext.CommandHandler` and the custom-conditions
        :class:`~telegram.ext.ConversationHandler`).
    """

    log = logger or get_logger(__name__)

    # -- /presets ------------------------------------------------------- #

    async def _show_presets(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List default + custom presets and offer the pick keyboard (Req 4.1, 4.3)."""

        message = update.effective_message
        if message is None:
            return
        user_data = context.user_data
        user_id = user_data.get(USER_ID_KEY) if user_data is not None else None
        if not isinstance(user_id, int):
            await message.reply_text(_NO_USER_TEXT)
            return

        region = user_data.get(REGION_KEY) if user_data is not None else None
        region_str = region if isinstance(region, str) else None

        options = await preset_service.list_presets(user_id, region=region_str)
        text = format_presets_list(options)
        if options:
            await message.reply_text(
                text, reply_markup=build_preset_pick_keyboard(options)
            )
        else:
            await message.reply_text(text)

    # -- custom-conditions conversation --------------------------------- #

    def _draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
        """Return (creating if needed) the in-progress draft dict."""

        user_data = context.user_data
        if user_data is None:
            # No per-user store (e.g. outside a user context); use a throwaway.
            return {}
        draft = user_data.get(_DRAFT_KEY)
        if not isinstance(draft, dict):
            draft = {}
            user_data[_DRAFT_KEY] = draft
        return draft

    def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
        """Drop the draft and the subscription handoff key once the flow ends."""

        user_data = context.user_data
        if user_data is not None:
            user_data.pop(_DRAFT_KEY, None)
            user_data.pop(SUBSCRIPTION_ID_KEY, None)

    async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Enter the conversation: require a target subscription, then prompt."""

        message = update.effective_message
        if message is None:
            return ConversationHandler.END

        user_data = context.user_data
        subscription_id = (
            user_data.get(SUBSCRIPTION_ID_KEY) if user_data is not None else None
        )
        if not isinstance(subscription_id, int):
            await message.reply_text(_NO_SUBSCRIPTION_TEXT)
            return ConversationHandler.END

        # Fresh draft for this run; preserve the subscription handoff key.
        if user_data is not None:
            user_data[_DRAFT_KEY] = {}
        await message.reply_text(_INTRO_TEXT)
        await message.reply_text(_PROMPT_MIN_WAVE)
        return CustomConditionsState.MIN_WAVE

    async def _on_min_wave(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Collect the minimum wave height."""

        message = update.effective_message
        if message is None or message.text is None:
            return CustomConditionsState.MIN_WAVE
        value = _parse_nonneg_float(message.text)
        if value is None:
            await message.reply_text(_INVALID_NUMBER)
            return CustomConditionsState.MIN_WAVE
        _draft(context)["min_wave_m"] = value
        await message.reply_text(_PROMPT_MAX_WAVE)
        return CustomConditionsState.MAX_WAVE

    async def _on_max_wave(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Collect the maximum wave height, rejecting an inverted band (Req 4.8)."""

        message = update.effective_message
        if message is None or message.text is None:
            return CustomConditionsState.MAX_WAVE
        value = _parse_nonneg_float(message.text)
        if value is None:
            await message.reply_text(_INVALID_NUMBER)
            return CustomConditionsState.MAX_WAVE

        draft = _draft(context)
        min_wave = draft.get("min_wave_m")
        # Inline wave-band guard so the offending value is re-requested at once
        # (Req 4.8); the final build re-validates as a safety net.
        if isinstance(min_wave, (int, float)) and value < float(min_wave):
            await message.reply_text(
                _INVALID_WAVE_BAND.format(min_wave=float(min_wave))
            )
            return CustomConditionsState.MAX_WAVE

        draft["max_wave_m"] = value
        await message.reply_text(_PROMPT_MIN_PERIOD)
        return CustomConditionsState.MIN_PERIOD

    async def _on_min_period(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Collect the minimum swell period."""

        message = update.effective_message
        if message is None or message.text is None:
            return CustomConditionsState.MIN_PERIOD
        value = _parse_nonneg_float(message.text)
        if value is None:
            await message.reply_text(_INVALID_NUMBER)
            return CustomConditionsState.MIN_PERIOD
        _draft(context)["min_period_s"] = value
        await message.reply_text(_PROMPT_MAX_WIND)
        return CustomConditionsState.MAX_WIND

    async def _on_max_wind(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Collect the maximum acceptable wind speed."""

        message = update.effective_message
        if message is None or message.text is None:
            return CustomConditionsState.MAX_WIND
        value = _parse_nonneg_float(message.text)
        if value is None:
            await message.reply_text(_INVALID_NUMBER)
            return CustomConditionsState.MAX_WIND
        _draft(context)["max_wind_kmh"] = value
        await message.reply_text(_PROMPT_WIND_DIR)
        return CustomConditionsState.WIND_DIR

    async def _on_wind_dir(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Collect the acceptable wind direction (compass, degrees, or skip)."""

        message = update.effective_message
        if message is None or message.text is None:
            return CustomConditionsState.WIND_DIR
        ok, degrees = _parse_direction(message.text)
        if not ok:
            await message.reply_text(_INVALID_DIRECTION)
            return CustomConditionsState.WIND_DIR
        _draft(context)["preferred_wind_dir_deg"] = degrees
        await message.reply_text(_PROMPT_SWELL_DIR)
        return CustomConditionsState.SWELL_DIR

    async def _on_swell_dir(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Collect the acceptable swell direction (compass, degrees, or skip)."""

        message = update.effective_message
        if message is None or message.text is None:
            return CustomConditionsState.SWELL_DIR
        ok, degrees = _parse_direction(message.text)
        if not ok:
            await message.reply_text(_INVALID_DIRECTION)
            return CustomConditionsState.SWELL_DIR
        _draft(context)["preferred_swell_dir_deg"] = degrees
        await message.reply_text(_PROMPT_TIDE)
        return CustomConditionsState.TIDE

    async def _on_tide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Collect the optional tide preference."""

        message = update.effective_message
        if message is None or message.text is None:
            return CustomConditionsState.TIDE
        ok, tide = _parse_tide(message.text)
        if not ok:
            await message.reply_text(_INVALID_TIDE)
            return CustomConditionsState.TIDE
        _draft(context)["tide_preference"] = tide
        await message.reply_text(_PROMPT_DAYLIGHT)
        return CustomConditionsState.DAYLIGHT

    async def _on_daylight(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Collect the daylight-only flag, then build and persist (Req 4.6, 4.8)."""

        message = update.effective_message
        if message is None or message.text is None:
            return CustomConditionsState.DAYLIGHT
        daylight = _parse_bool(message.text)
        if daylight is None:
            await message.reply_text(_INVALID_BOOL)
            return CustomConditionsState.DAYLIGHT

        draft = _draft(context)
        draft["daylight_only"] = daylight

        try:
            conditions = surf_conditions_from_fields(
                min_wave_m=float(draft["min_wave_m"]),
                max_wave_m=float(draft["max_wave_m"]),
                min_period_s=float(draft["min_period_s"]),
                max_wind_kmh=float(draft["max_wind_kmh"]),
                preferred_wind_dir_deg=draft.get("preferred_wind_dir_deg"),
                preferred_swell_dir_deg=draft.get("preferred_swell_dir_deg"),
                tide_preference=draft.get("tide_preference"),
                daylight_only=daylight,
            )
        except DomainValidationError as exc:
            # Safety net: surface the rule and restart from the wave band.
            await message.reply_text(f"{exc}\n\n{_PROMPT_MIN_WAVE}")
            if context.user_data is not None:
                context.user_data[_DRAFT_KEY] = {}
            return CustomConditionsState.MIN_WAVE

        user_data = context.user_data
        subscription_id = (
            user_data.get(SUBSCRIPTION_ID_KEY) if user_data is not None else None
        )
        if not isinstance(subscription_id, int):
            await message.reply_text(_NO_SUBSCRIPTION_TEXT)
            _clear(context)
            return ConversationHandler.END

        await preset_service.create_custom_conditions(subscription_id, conditions)
        log.bind(subscription_id=subscription_id).info(
            "custom conditions saved via conversation"
        )
        await message.reply_text(_SAVED_TEXT)
        _clear(context)
        return ConversationHandler.END

    async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Abort the conversation, discarding any in-progress draft."""

        message = update.effective_message
        if message is not None:
            await message.reply_text(_CANCELLED_TEXT)
        _clear(context)
        return ConversationHandler.END

    _text = filters.TEXT & ~filters.COMMAND

    custom_conditions = ConversationHandler(
        entry_points=[CommandHandler("customconditions", _start)],
        states={
            CustomConditionsState.MIN_WAVE: [MessageHandler(_text, _on_min_wave)],
            CustomConditionsState.MAX_WAVE: [MessageHandler(_text, _on_max_wave)],
            CustomConditionsState.MIN_PERIOD: [MessageHandler(_text, _on_min_period)],
            CustomConditionsState.MAX_WIND: [MessageHandler(_text, _on_max_wind)],
            CustomConditionsState.WIND_DIR: [MessageHandler(_text, _on_wind_dir)],
            CustomConditionsState.SWELL_DIR: [MessageHandler(_text, _on_swell_dir)],
            CustomConditionsState.TIDE: [MessageHandler(_text, _on_tide)],
            CustomConditionsState.DAYLIGHT: [MessageHandler(_text, _on_daylight)],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        name="custom_conditions",
    )

    return [
        CommandHandler("presets", _show_presets),
        custom_conditions,
    ]
