"""Subscription command and conversation handlers — ReplyKeyboard UX.

All keyboards are ReplyKeyboard (bottom navigation) for a consistent feel.

Flow
----
📋 My subscriptions →
  Shows subscriptions as reply buttons + ➕ Subscribe button.
  Tap subscription → detail keyboard (🌊 Forecast, 🗑️ Remove, ⬅️ Back).
  Tap ➕ Subscribe → share location or type city → subscription created.

Requirements covered: 3.1, 3.4, 3.5, 3.6, 3.8.
"""

from __future__ import annotations

from typing import Any, Final, cast

from telegram import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from brizocast.activities.registry import ActivityRegistry
from brizocast.bot.keyboards.common import (
    build_confirm_keyboard,
    parse_confirm_callback,
)
from brizocast.bot.keyboards.menu import (
    MENU_LABEL_ADD,
    MENU_LABEL_SUBSCRIPTIONS,
    any_menu_label_filter,
    build_main_menu_keyboard,
    menu_filter,
)
from brizocast.bot.formatters.commands import (
    format_forecast_no_spots,
    format_forecast_result,
)
from brizocast.core.errors import DomainValidationError, NotFoundError, ProviderRequestError
from brizocast.core.logging import get_logger
from brizocast.services.location_service import LocationService
from brizocast.services.spot_ingestion_service import SpotIngestionService
from brizocast.services.status_service import BestForecast, DailyForecast, StatusService
from brizocast.services.subscription_service import SubscriptionService
from brizocast.services.user_service import UserService

# forward ref
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from brizocast.services.preset_service import PresetService
    from brizocast.services.spot_discovery_service import SpotDiscoveryService

__all__ = [
    "CTX_ACTIVITY_IDS",
    "CTX_DB_USER_ID",
    "CTX_PENDING_CUSTOM_SUBSCRIPTION_ID",
    "build_subscription_handlers",
]

_log = get_logger(__name__)

# --- cross-handler context keys ---------------------------------------- #
CTX_DB_USER_ID: Final = "db_user_id"
CTX_ACTIVITY_IDS: Final = "activity_ids"
CTX_PENDING_CUSTOM_SUBSCRIPTION_ID: Final = "pending_custom_subscription_id"

_CTX_ADD_DRAFT: Final = "add_subscription_draft"
# user_data key: list of (label, subscription_id) for the current list view
_CTX_SUB_MAP: Final = "sub_label_map"

# --- conversation states ----------------------------------------------- #
(
    _LIST,
    _DETAIL,
    _DETAIL_CONFIRM_REMOVE,
    _ADD_NEW_LOCATION,
    _ADD_PICK_CANDIDATE,
    _SETTINGS_MENU,
    _SETTINGS_WAVE,
    _SETTINGS_SCORE,
    _SETTINGS_WIND,
    _SETTINGS_ENERGY,
) = range(10)

# --- button labels ------- #
_BTN_SUBSCRIBE: Final = "➕ Subscribe"
_BTN_FORECAST: Final = "🌊 Forecast"
_BTN_SETTINGS: Final = "⚙️ Alert settings"
_BTN_REMOVE: Final = "🗑️ Remove"
_BTN_BACK: Final = "⬅️ Back"
_BTN_CONFIRM_YES: Final = "✅ Yes, remove"
_BTN_CONFIRM_NO: Final = "❌ No, keep it"

# Alert settings buttons
_BTN_SET_WAVE: Final = "🌊 Min wave height"
_BTN_SET_SCORE: Final = "⭐ Min score"
_BTN_SET_WIND: Final = "💨 Max wind"
_BTN_SET_ENERGY: Final = "⚡ Min energy (kW/m)"
_BTN_SETTINGS_SAVE: Final = "✅ Save settings"
_BTN_SETTINGS_RESET: Final = "🔄 Reset to default"

_REMOVE_CONFIRM_PATTERN: Final = r"^cf:1:[yn]:remove:"
_END: Final = ConversationHandler.END


# ----------------------------------------------------------------------- #
# helpers
# ----------------------------------------------------------------------- #
def _user_data(context: ContextTypes.DEFAULT_TYPE) -> dict[Any, Any]:
    data = context.user_data
    if data is None:  # pragma: no cover
        raise RuntimeError("no user_data")
    return data


def _db_user_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    raw = _user_data(context).get(CTX_DB_USER_ID)
    return raw if isinstance(raw, int) else None


def _resolve_activity_id(context: ContextTypes.DEFAULT_TYPE, key: str) -> int | None:
    bot_data = context.bot_data
    if bot_data is None:  # pragma: no cover
        return None
    mapping = bot_data.get(CTX_ACTIVITY_IDS)
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(key)
    return value if isinstance(value, int) else None


def _clear_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    _user_data(context).pop(_CTX_ADD_DRAFT, None)


async def _send(
    update: Update,
    text: str,
    keyboard: ReplyKeyboardMarkup | ReplyKeyboardRemove | InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str | None = None,
) -> None:
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode)


def _sub_list_keyboard(summaries: list[Any]) -> ReplyKeyboardMarkup:
    """ReplyKeyboard: one button per subscription + ➕ Subscribe row."""
    rows: list[list[str]] = []
    for s in summaries:
        label = s.location_label or s.location_place or f"#{s.subscription_id}"
        rows.append([f"🏄 {label}"])
    rows.append([_BTN_SUBSCRIBE])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _detail_keyboard() -> ReplyKeyboardMarkup:
    """ReplyKeyboard for subscription detail: Forecast, Settings, Remove, Back."""
    return ReplyKeyboardMarkup(
        [[_BTN_FORECAST, _BTN_SETTINGS], [_BTN_REMOVE, _BTN_BACK]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _settings_keyboard() -> ReplyKeyboardMarkup:
    """ReplyKeyboard for alert settings menu."""
    return ReplyKeyboardMarkup(
        [[_BTN_SET_WAVE, _BTN_SET_SCORE], [_BTN_SET_WIND, _BTN_SET_ENERGY], [_BTN_SETTINGS_RESET, _BTN_BACK]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _confirm_keyboard() -> ReplyKeyboardMarkup:
    """ReplyKeyboard for remove confirmation."""
    return ReplyKeyboardMarkup(
        [[_BTN_CONFIRM_YES, _BTN_CONFIRM_NO]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _location_keyboard() -> ReplyKeyboardMarkup:
    """ReplyKeyboard with native share-location button."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share my location", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _sub_button_label(s: Any) -> str:
    base = s.location_label or s.location_place or f"#{s.subscription_id}"
    return f"🏄 {base}"


# ----------------------------------------------------------------------- #
# builder
# ----------------------------------------------------------------------- #
def build_subscription_handlers(
    subscription_service: SubscriptionService,
    location_service: LocationService,
    user_service: UserService,
    *,
    status_service: StatusService | None = None,
    preset_service: "PresetService | None" = None,
    spot_discovery: "SpotDiscoveryService | None" = None,
    spot_ingestion: SpotIngestionService | None = None,
    ingest_radius_km: float = 50.0,
) -> list[BaseHandler[Any, Any, Any]]:

    async def _resolve_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
        user_id = _db_user_id(context)
        if user_id is not None:
            return user_id
        tg_user = update.effective_user
        if tg_user is None:
            return None
        user = await user_service.get_or_create_user(tg_user.id, tg_user.username)
        _user_data(context)[CTX_DB_USER_ID] = user.id
        return user.id

    # ------------------------------------------------------------------- #
    # LIST — show subscriptions as reply buttons
    # ------------------------------------------------------------------- #
    async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = await _resolve_user(update, context)
        if user_id is None:
            await _send(update, _NEED_START_TEXT)
            return _END
        summaries = await subscription_service.summarize_for_user(user_id)

        # Store label→id map so taps can resolve the subscription
        _user_data(context)[_CTX_SUB_MAP] = {
            _sub_button_label(s): s.subscription_id for s in summaries
        }

        text = "📋 Your subscriptions:" if summaries else "📋 No subscriptions yet."
        await _send(update, text, _sub_list_keyboard(summaries))
        return _LIST

    async def list_tapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle a button tap in the list state — either a subscription or ➕ Subscribe."""
        msg = update.effective_message
        text = msg.text.strip() if msg and msg.text else ""

        if text == _BTN_SUBSCRIBE:
            return await _add_start(update, context)

        # Subscription button tapped — find it in the map
        sub_map: dict[str, int] = _user_data(context).get(_CTX_SUB_MAP, {})
        sub_id = sub_map.get(text)
        if sub_id is None:
            # Unknown button — re-show list
            return await show_list(update, context)

        # Store which subscription we're viewing
        _user_data(context)["_current_sub_id"] = sub_id

        user_id = _db_user_id(context)
        assert user_id is not None
        summaries = await subscription_service.summarize_for_user(user_id)
        summary = next((s for s in summaries if s.subscription_id == sub_id), None)
        if summary is None:
            return await show_list(update, context)

        detail_text = (
            f"🏖️ {summary.location_label or summary.location_place}\n"
            f"Radius: {summary.search_radius_km:g} km · Notify: {summary.notification_mode}"
        )
        await _send(update, detail_text, _detail_keyboard())
        return _DETAIL

    # ------------------------------------------------------------------- #
    # DETAIL — Forecast / Remove / Back
    # ------------------------------------------------------------------- #
    async def detail_tapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        text = msg.text.strip() if msg and msg.text else ""
        sub_id: int | None = _user_data(context).get("_current_sub_id")

        if text == _BTN_BACK:
            return await show_list(update, context)

        if text == _BTN_FORECAST:
            if sub_id is None:
                return await show_list(update, context)
            if status_service is not None:
                try:
                    daily = await status_service.daily_forecast_for_subscription(sub_id)
                    await _send(update, _render_daily_forecast(daily), _detail_keyboard())
                except NotFoundError:
                    await _send(update, "Subscription not found.", _detail_keyboard())
            else:
                await _send(update, "🌊 Use /forecast for an up-to-date forecast.", _detail_keyboard())
            return _DETAIL

        if text == _BTN_REMOVE:
            if sub_id is None:
                return await show_list(update, context)
            await _send(
                update,
                "Remove this subscription? This cannot be undone.",
                _confirm_keyboard(),
            )
            return _DETAIL_CONFIRM_REMOVE

        if text == _BTN_SETTINGS:
            if sub_id is None:
                return await show_list(update, context)
            return await _show_settings_menu(update, context, sub_id)

        # Unknown — stay in detail
        return _DETAIL

    # ------------------------------------------------------------------- #
    # CONFIRM REMOVE
    # ------------------------------------------------------------------- #
    async def confirm_remove_tapped(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        msg = update.effective_message
        text = msg.text.strip() if msg and msg.text else ""
        sub_id: int | None = _user_data(context).get("_current_sub_id")

        if text == _BTN_CONFIRM_NO or sub_id is None:
            # Go back to detail
            user_id = _db_user_id(context)
            assert user_id is not None
            summaries = await subscription_service.summarize_for_user(user_id)
            summary = next((s for s in summaries if s.subscription_id == sub_id), None) if sub_id else None
            if summary:
                detail_text = (
                    f"🏖️ {summary.location_label or summary.location_place}\n"
                    f"Radius: {summary.search_radius_km:g} km · Notify: {summary.notification_mode}"
                )
                await _send(update, detail_text, _detail_keyboard())
                return _DETAIL
            return await show_list(update, context)

        if text == _BTN_CONFIRM_YES and sub_id is not None:
            try:
                await subscription_service.remove(sub_id)
            except NotFoundError:
                pass
            _user_data(context).pop("_current_sub_id", None)
            await _send(update, "🗑️ Removed.")
            return await show_list(update, context)

        return _DETAIL_CONFIRM_REMOVE

    # ------------------------------------------------------------------- #
    # ALERT SETTINGS
    # ------------------------------------------------------------------- #
    async def _show_settings_menu(
        update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int
    ) -> int:
        """Show current alert settings — AI-generated regional defaults when available."""
        if preset_service is None:
            await _send(update, "⚙️ Alert settings\n\nSet your conditions:", _settings_keyboard())
            return _SETTINGS_MENU

        subs = await subscription_service.list_for_user(cast(int, _db_user_id(context)))
        sub = next((s for s in subs if s.id == sub_id), None)
        if sub is None:
            await _send(update, "⚙️ Alert settings\n\nSet your conditions:", _settings_keyboard())
            return _SETTINGS_MENU

        # Resolve region from nearest spot if we have spot_discovery
        region: str | None = None
        if spot_discovery is not None:
            target = await subscription_service.get_forecast_target(sub_id)
            if target is not None:
                result = spot_discovery.discover(target.center, target.search_radius_km)
                if result.has_nearby_spots:
                    region = result.spots[0].region

        # Get effective conditions (custom > preset > AI/regional default)
        conditions = await preset_service.resolve_effective_conditions(sub, region=region)

        # Check if there's a custom override
        has_custom = False
        from brizocast.repositories.condition_repo import SqlAlchemyCustomConditionRepository
        from brizocast.database.session import session_scope
        async with session_scope(preset_service._session_factory) as sess:
            repo = SqlAlchemyCustomConditionRepository(sess)
            has_custom = await repo.get_for_subscription(sub_id) is not None

        # Get AI-suggested regional defaults for display
        ai_label = ""
        if not has_custom and region:
            try:
                ai_options = await preset_service.get_region_presets(region)
                if ai_options:
                    ai_p = ai_options[0].params
                    ai_label = (
                        f"\n\n🤖 AI default for {region}:\n"
                        f"  Wave {ai_p.min_wave_m:.1f}–{ai_p.max_wave_m:.1f}m · "
                        f"Period {ai_p.min_period_s:.0f}s · "
                        f"Wind ≤{ai_p.max_wind_kmh:.0f} km/h"
                    )
            except Exception:  # noqa: BLE001
                pass

        source = "Custom" if has_custom else f"Default{' (AI)' if region else ''}"
        # Show threshold: custom → preset → default 50
        preset_score: int | None = None
        if not has_custom and sub:
            if sub.preset_id and preset_service is not None:
                from brizocast.repositories.preset_repo import SqlAlchemyPresetRepository
                from brizocast.database.session import session_scope
                async with session_scope(preset_service._session_factory) as ps:
                    pr = SqlAlchemyPresetRepository(ps)
                    p = await pr.get(sub.preset_id)
                    if p:
                        preset_score = getattr(p, "min_alert_score", None)
        score_thresh = _user_data(context).get(f"_settings_{sub_id}_min_alert_score") or preset_score or 50
        energy_thresh = _user_data(context).get(f"_settings_{sub_id}_min_energy")
        energy_str = f"\n⚡ Min energy: {energy_thresh:.0f} kW/m" if energy_thresh else ""
        text = (
            f"⚙️ Alert settings\n"
            f"Source: {source}\n\n"
            f"🌊 Min wave: {conditions.min_wave_m:.1f}m\n"
            f"💨 Max wind: {conditions.max_wind_kmh:.0f} km/h\n"
            f"⭐ Min score: {score_thresh}"
            f"{energy_str}"
            f"{ai_label}\n\n"
            "Tap a setting to override."
        )
        await _send(update, text, _settings_keyboard())
        return _SETTINGS_MENU

    async def settings_tapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        text = msg.text.strip() if msg and msg.text else ""
        sub_id: int | None = _user_data(context).get("_current_sub_id")

        if text == _BTN_BACK:
            if sub_id is None:
                return await show_list(update, context)
            # Back to subscription detail
            subs = await subscription_service.summarize_for_user(cast(int, _db_user_id(context)))
            summary = next((s for s in subs if s.subscription_id == sub_id), None)
            if summary:
                detail_text = (
                    f"🏖️ {summary.location_label or summary.location_place}\n"
                    f"Radius: {summary.search_radius_km:g} km · Notify: {summary.notification_mode}"
                )
                await _send(update, detail_text, _detail_keyboard())
            return _DETAIL

        if text == _BTN_SET_WAVE:
            await _send(update, "🌊 Enter minimum wave height in metres (e.g. 1.0):")
            return _SETTINGS_WAVE

        if text == _BTN_SET_SCORE:
            await _send(update, "⭐ Enter minimum score 0-100 (e.g. 60):")
            return _SETTINGS_SCORE

        if text == _BTN_SET_WIND:
            await _send(update, "💨 Enter maximum wind speed in km/h (e.g. 25):")
            return _SETTINGS_WIND

        if text == _BTN_SET_ENERGY:
            await _send(update, "⚡ Enter minimum wave energy in kW/m (e.g. 10):\n\n"
                        "Reference: ~5 = weak, ~15 = moderate, ~30 = powerful")
            return _SETTINGS_ENERGY

        if text == _BTN_SETTINGS_RESET:
            if sub_id and preset_service is not None:
                from brizocast.repositories.condition_repo import SqlAlchemyCustomConditionRepository
                from brizocast.database.session import session_scope
                async with session_scope(preset_service._session_factory) as sess:
                    repo = SqlAlchemyCustomConditionRepository(sess)
                    existing = await repo.get_for_subscription(sub_id)
                    if existing is not None:
                        await repo.delete(sub_id)
            await _send(update, "✅ Reset to default conditions.", _settings_keyboard())
            return _SETTINGS_MENU

        return _SETTINGS_MENU

    async def settings_wave_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        text = msg.text.strip() if msg and msg.text else ""
        sub_id: int | None = _user_data(context).get("_current_sub_id")
        try:
            val = float(text)
            if val <= 0:
                raise ValueError
        except ValueError:
            await _send(update, "❌ Invalid. Enter a positive number (e.g. 1.0):")
            return _SETTINGS_WAVE
        _user_data(context)["_settings_wave"] = val
        if sub_id and preset_service is not None:
            await _save_conditions(context, sub_id)
        await _send(update, f"✅ Min wave set to {val:.1f}m", _settings_keyboard())
        return _SETTINGS_MENU

    async def settings_score_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        text = msg.text.strip() if msg and msg.text else ""
        sub_id: int | None = _user_data(context).get("_current_sub_id")
        try:
            val = int(text)
            if not 0 <= val <= 100:
                raise ValueError
        except ValueError:
            await _send(update, "❌ Invalid. Enter a number 0-100:")
            return _SETTINGS_SCORE
        _user_data(context)[f"_settings_{sub_id}_min_alert_score"] = val
        # Persist min_alert_score in custom_condition row
        if sub_id and preset_service is not None:
            from brizocast.database.session import session_scope
            from brizocast.repositories.condition_repo import SqlAlchemyCustomConditionRepository
            subs = await subscription_service.list_for_user(cast(int, _db_user_id(context)))
            sub_obj = next((s for s in subs if s.id == sub_id), None)
            if sub_obj:
                current = await preset_service.resolve_effective_conditions(sub_obj)
                from brizocast.services.preset_service import surf_conditions_from_fields
                try:
                    new_cond = surf_conditions_from_fields(
                        min_wave_m=current.min_wave_m,
                        max_wave_m=max(current.min_wave_m + 2.0, current.max_wave_m),
                        min_period_s=current.min_period_s,
                        max_wind_kmh=current.max_wind_kmh,
                    )
                    await preset_service.create_custom_conditions(sub_id, new_cond)
                    async with session_scope(preset_service._session_factory) as sess:
                        repo = SqlAlchemyCustomConditionRepository(sess)
                        existing = await repo.get_for_subscription(sub_id)
                        if existing is not None:
                            setattr(existing, "min_alert_score", val)
                            await repo.update(existing)
                except Exception:  # noqa: BLE001
                    pass
        await _send(update, f"✅ Min score set to {val}", _settings_keyboard())
        return _SETTINGS_MENU

    async def settings_wind_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        text = msg.text.strip() if msg and msg.text else ""
        sub_id: int | None = _user_data(context).get("_current_sub_id")
        try:
            val = float(text)
            if val <= 0:
                raise ValueError
        except ValueError:
            await _send(update, "❌ Invalid. Enter a positive number (e.g. 25):")
            return _SETTINGS_WIND
        _user_data(context)["_settings_wind"] = val
        if sub_id and preset_service is not None:
            await _save_conditions(context, sub_id)
        await _send(update, f"✅ Max wind set to {val:.0f} km/h", _settings_keyboard())
        return _SETTINGS_MENU

    async def settings_energy_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        text = msg.text.strip() if msg and msg.text else ""
        sub_id: int | None = _user_data(context).get("_current_sub_id")
        try:
            val = float(text)
            if val < 0:
                raise ValueError
        except ValueError:
            await _send(update, "❌ Invalid. Enter a non-negative number (e.g. 10):")
            return _SETTINGS_ENERGY
        _user_data(context)[f"_settings_{sub_id}_min_energy"] = val
        # Persist in custom_condition
        if sub_id and preset_service is not None:
            from brizocast.database.session import session_scope
            from brizocast.repositories.condition_repo import SqlAlchemyCustomConditionRepository
            subs = await subscription_service.list_for_user(cast(int, _db_user_id(context)))
            sub_obj = next((s for s in subs if s.id == sub_id), None)
            if sub_obj:
                current = await preset_service.resolve_effective_conditions(sub_obj)
                from brizocast.services.preset_service import surf_conditions_from_fields
                try:
                    new_cond = surf_conditions_from_fields(
                        min_wave_m=current.min_wave_m,
                        max_wave_m=max(current.min_wave_m + 2.0, current.max_wave_m),
                        min_period_s=current.min_period_s,
                        max_wind_kmh=current.max_wind_kmh,
                    )
                    cc = await preset_service.create_custom_conditions(sub_id, new_cond)
                    # Set min_energy directly on the row
                    async with session_scope(preset_service._session_factory) as sess:
                        repo = SqlAlchemyCustomConditionRepository(sess)
                        existing = await repo.get_for_subscription(sub_id)
                        if existing is not None:
                            setattr(existing, "min_energy_kw", val)
                            await repo.update(existing)
                except Exception:  # noqa: BLE001
                    pass
        await _send(update, f"✅ Min energy set to {val:.0f} kW/m", _settings_keyboard())
        return _SETTINGS_MENU

    async def _save_conditions(
        context: ContextTypes.DEFAULT_TYPE, sub_id: int
    ) -> None:
        """Save custom conditions from user_data to DB."""
        if preset_service is None:
            return
        from brizocast.activities.surf.conditions import SurfConditions
        from brizocast.services.preset_service import surf_conditions_from_fields

        subs = await subscription_service.list_for_user(cast(int, _db_user_id(context)))
        sub = next((s for s in subs if s.id == sub_id), None)
        if sub is None:
            return
        current = await preset_service.resolve_effective_conditions(sub)
        wave = _user_data(context).get("_settings_wave", current.min_wave_m)
        wind = _user_data(context).get("_settings_wind", current.max_wind_kmh)
        try:
            new_cond = surf_conditions_from_fields(
                min_wave_m=float(wave),
                max_wave_m=max(float(wave) + 2.0, current.max_wave_m),
                min_period_s=current.min_period_s,
                max_wind_kmh=float(wind),
            )
            await preset_service.create_custom_conditions(sub_id, new_cond)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------- #
    # ADD — location capture
    # ------------------------------------------------------------------- #
    async def _add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _db_user_id(context)
        assert user_id is not None
        locations = await location_service.list_favorites(user_id)

        if locations:
            # Has saved locations — show them as reply buttons
            rows: list[list[str]] = [[loc.label or loc.city or f"{loc.lat:.3f},{loc.lon:.3f}"] for loc in locations]
            rows.append(["📍 New location"])
            _user_data(context)["_loc_map"] = {
                (loc.label or loc.city or f"{loc.lat:.3f},{loc.lon:.3f}"): loc.id
                for loc in locations
            }
            await _send(
                update,
                "📍 Choose a saved location or add a new one:",
                ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True),
            )
            return _ADD_NEW_LOCATION

        # No saved locations — ask to share/type
        await _send(
            update,
            "📍 Share your location or type a city name:",
            _location_keyboard(),
        )
        return _ADD_NEW_LOCATION

    async def add_location_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        text = msg.text.strip() if msg and msg.text else ""

        # Check if it's a saved location button
        loc_map: dict[str, int] = _user_data(context).get("_loc_map", {})
        if text in loc_map:
            loc_id = loc_map[text]
            _user_data(context).pop("_loc_map", None)
            # Ensure AI presets exist for this region (cache-bypassing)
            if spot_ingestion is not None:
                user_id = _db_user_id(context)
                if user_id is not None:
                    saved_locs = await location_service.list_favorites(user_id)
                    saved_loc = next((l for l in saved_locs if l.id == loc_id), None)
                    if saved_loc is not None:
                        await spot_ingestion.ensure_region_presets(saved_loc.lat, saved_loc.lon)
            return await _create_subscription_for_location(update, context, loc_id)

        if text == "📍 New location":
            await _send(update, "📍 Share your location or type a city name:", _location_keyboard())
            return _ADD_NEW_LOCATION

        if not text:
            return _ADD_NEW_LOCATION

        # Treat as city search
        if msg:
            await msg.chat.send_action("typing")
        try:
            candidates = await location_service.search(text)
        except ProviderRequestError:
            await _send(update, "Search unavailable. Try sharing your GPS location.")
            return _ADD_NEW_LOCATION

        if not candidates:
            await _send(update, "No places found. Try a different name.")
            return _ADD_NEW_LOCATION

        if len(candidates) == 1:
            user_id = _db_user_id(context)
            assert user_id is not None
            location = await location_service.create_from_candidate(user_id, candidates[0], is_favorite=True)
            if spot_ingestion is not None:
                await spot_ingestion.ingest_near(location.lat, location.lon, ingest_radius_km)
            return await _create_subscription_for_location(update, context, location.id)

        # Multiple candidates — show as reply buttons
        _user_data(context)["_candidates"] = list(candidates)
        rows = [[f"{c.name}, {c.country or c.city or ''}" for c in candidates[i:i+2]]
                for i in range(0, len(candidates), 2)]
        await _send(
            update,
            "Pick the right place:",
            ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True),
        )
        return _ADD_PICK_CANDIDATE

    async def add_location_shared(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        assert msg is not None and msg.location is not None
        user_id = _db_user_id(context)
        assert user_id is not None
        await msg.chat.send_action("typing")
        location = await location_service.create_from_coordinates(
            user_id, msg.location.latitude, msg.location.longitude, is_favorite=True
        )
        if spot_ingestion is not None:
            await spot_ingestion.ingest_near(msg.location.latitude, msg.location.longitude, ingest_radius_km)
        return await _create_subscription_for_location(update, context, location.id)

    async def add_pick_candidate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        text = msg.text.strip() if msg and msg.text else ""
        candidates = _user_data(context).get("_candidates", [])

        # Find matching candidate by label
        chosen = next(
            (c for c in candidates if f"{c.name}, {c.country or c.city or ''}" == text),
            None,
        )
        if chosen is None:
            await _send(update, "Couldn't find that. Try again.")
            return _ADD_PICK_CANDIDATE

        user_id = _db_user_id(context)
        assert user_id is not None
        _user_data(context).pop("_candidates", None)
        location = await location_service.create_from_candidate(user_id, chosen, is_favorite=True)
        if spot_ingestion is not None:
            await spot_ingestion.ingest_near(location.lat, location.lon, ingest_radius_km)
        return await _create_subscription_for_location(update, context, location.id)

    async def _create_subscription_for_location(
        update: Update, context: ContextTypes.DEFAULT_TYPE, location_id: int
    ) -> int:
        user_id = _db_user_id(context)
        assert user_id is not None
        activity_id = _resolve_activity_id(context, "surf")
        if activity_id is None:
            await _send(update, _ACTIVITY_UNKNOWN_TEXT)
            return _END

        # Show progress to user while we search for spots.
        msg = update.effective_message
        if msg:
            progress = await msg.reply_text("🔍 Searching for surf spots nearby…")
        else:
            progress = None

        # Run spot ingestion if configured (imports from Surfline catalog).
        from brizocast.core.domain.geo import GeoPoint
        from brizocast.models.location import Location as LocModel
        from brizocast.database.session import session_scope

        loc_point: GeoPoint | None = None
        ingest_radius = ingest_radius_km if spot_ingestion else 50.0

        try:
            async with session_scope(subscription_service._session_factory) as _s:
                loc = await _s.get(LocModel, location_id)
                if loc is not None:
                    loc_point = GeoPoint(lat=loc.lat, lon=loc.lon)
        except Exception:  # noqa: BLE001
            pass

        if loc_point and spot_ingestion:
            try:
                await spot_ingestion.ingest_near(loc_point.lat, loc_point.lon, ingest_radius)
            except Exception:  # noqa: BLE001
                pass

        # Discover nearby spots and auto-attach regional preset.
        preset_id_to_use: int | None = None
        nearby_spots: list[str] = []
        if preset_service is not None and spot_discovery is not None and loc_point:
            try:
                result = spot_discovery.discover(loc_point, 20.0)
                nearby_spots = [s.name for s in result.spots[:10]]
                region = result.spots[0].region if result.has_nearby_spots else None
                if region:
                    from brizocast.repositories.preset_repo import SqlAlchemyPresetRepository
                    async with session_scope(preset_service._session_factory) as _s2:
                        presets = await SqlAlchemyPresetRepository(_s2).list_defaults(region)
                        if presets:
                            preset_id_to_use = presets[0].id
            except Exception:  # noqa: BLE001
                pass

        try:
            subscription = await subscription_service.create(
                user_id, activity_id, location_id,
                search_radius_km=None, preset_id=preset_id_to_use, notification_mode="immediate",
            )
        except DomainValidationError as exc:
            if progress:
                await progress.delete()
            await _send(update, f"❌ {exc}")
            return _END

        # Delete the progress message.
        if progress:
            try:
                await progress.delete()
            except Exception:  # noqa: BLE001
                pass

        # Build a nice confirmation with the list of monitored spots.
        summaries = await subscription_service.summarize_for_user(user_id)
        created = next((s for s in summaries if s.subscription_id == subscription.id), None)
        location_name = (created.location_label or created.location_place or "your location") if created else "your location"
        radius = created.search_radius_km if created else 20

        if nearby_spots:
            spots_text = "\n".join(f"  • {name}" for name in nearby_spots)
            text = (
                f"✅ Subscribed to *{location_name}*\n\n"
                f"🏖 Monitoring {len(nearby_spots)} spot(s) within {radius:g} km:\n"
                f"{spots_text}\n\n"
                f"I'll alert you when conditions are good 🤙"
            )
        else:
            text = (
                f"✅ Subscribed to *{location_name}*\n\n"
                f"⚠️ No surf spots found within {radius:g} km yet.\n"
                f"Spots will appear once the catalog becomes available."
            )
        await _send(update, text, parse_mode="Markdown")
        return await show_list(update, context)

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _clear_draft(context)
        await _send(update, "Cancelled.", build_main_menu_keyboard())
        return _END

    # ------------------------------------------------------------------- #
    # text filter helpers
    # ------------------------------------------------------------------- #
    def _is_detail_btn(text: str) -> bool:
        return text in (_BTN_FORECAST, _BTN_REMOVE, _BTN_BACK)

    def _is_confirm_btn(text: str) -> bool:
        return text in (_BTN_CONFIRM_YES, _BTN_CONFIRM_NO)

    _detail_filter = filters.Regex(rf"^({_BTN_FORECAST}|{_BTN_SETTINGS}|{_BTN_REMOVE}|{_BTN_BACK})$")
    _confirm_filter = filters.Regex(rf"^({_BTN_CONFIRM_YES}|{_BTN_CONFIRM_NO})$")
    _text_filter = filters.TEXT & ~filters.COMMAND & ~any_menu_label_filter()
    _sub_tap_filter = filters.Regex(r"^🏄 ")  # any "🏄 ..." button = subscription tap

    handler = ConversationHandler(
        entry_points=[
            CommandHandler("subscriptions", show_list),
            CommandHandler("add", _add_start),
            MessageHandler(menu_filter(MENU_LABEL_SUBSCRIPTIONS), show_list),
            MessageHandler(menu_filter(MENU_LABEL_ADD), _add_start),
            # 🏄 taps are always valid entry points — re-shows list if state was lost
            MessageHandler(_sub_tap_filter, list_tapped),
        ],
        states={
            _LIST: [
                MessageHandler(_text_filter, list_tapped),
            ],
            _DETAIL: [
                MessageHandler(_detail_filter, detail_tapped),
                # Allow typing in detail state too (e.g. user presses Back)
                MessageHandler(_text_filter & ~_detail_filter, detail_tapped),
            ],
            _DETAIL_CONFIRM_REMOVE: [
                MessageHandler(_confirm_filter, confirm_remove_tapped),
                MessageHandler(_text_filter & ~_confirm_filter, confirm_remove_tapped),
            ],
            _ADD_NEW_LOCATION: [
                MessageHandler(filters.LOCATION, add_location_shared),
                MessageHandler(_text_filter, add_location_text),
            ],
            _ADD_PICK_CANDIDATE: [
                MessageHandler(_text_filter, add_pick_candidate),
            ],
            _SETTINGS_MENU: [
                MessageHandler(_text_filter, settings_tapped),
            ],
            _SETTINGS_WAVE: [
                MessageHandler(_text_filter, settings_wave_entered),
            ],
            _SETTINGS_SCORE: [
                MessageHandler(_text_filter, settings_score_entered),
            ],
            _SETTINGS_WIND: [
                MessageHandler(_text_filter, settings_wind_entered),
            ],
            _SETTINGS_ENERGY: [
                MessageHandler(_text_filter, settings_energy_entered),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(menu_filter(MENU_LABEL_SUBSCRIPTIONS), show_list),
            MessageHandler(_sub_tap_filter, list_tapped),
            MessageHandler(_detail_filter, show_list),  # stale detail buttons → back to list
        ],
        name="subscriptions",
        allow_reentry=True,
        persistent=False,
    )

    return [handler]


def _render_best_forecast(best: BestForecast) -> str:
    if best.has_result:
        assert best.spot is not None and best.score is not None
        category_label = best.category.name.title() if best.category is not None else ""
        return format_forecast_result(
            location_label=best.location_label,
            spot_name=best.spot.name,
            score=best.score,
            category_label=category_label,
        )
    return format_forecast_no_spots(best.location_label)


def _render_daily_forecast(daily: "DailyForecast") -> str:
    """Render daily forecast in morning/midday/evening sections."""
    from brizocast.bot.formatters.alerts import _compass, _score_stars, _swell_direction_arrow, _to_utc, _wave_energy, _weather_emoji, _wind_emoji

    if not daily.has_nearby_spots:
        return f"🔮 No surf spots found near {daily.location_label}."

    lines = [f"🌊 *{daily.location_label}* — today's forecast\n"]
    has_any = False
    for period in daily.periods:
        if period.spot is None or period.step is None or period.score is None:
            lines.append(f"{period.period_name}\n_No data_\n")
            continue
        has_any = True
        step = period.step
        wind_dir = _compass(step.wind_direction_deg)
        wind_arrow = _wind_emoji(step.wind_direction_deg)
        swell_arrow = _swell_direction_arrow(step.swell_direction_deg)
        swell_dir = _compass(step.swell_direction_deg)
        stars = _score_stars(period.score)
        cat = period.category.name.title() if period.category else ""
        ts = _to_utc(period.result.forecast_window.start).strftime("%H:%M") if period.result else "—"
        energy = 0.5 * step.wave_height_m ** 2 * step.swell_period_s
        energy_str = f"{energy:.0f} kW/m"
        weather = _weather_emoji(step.weather_code, step.temperature_c)
        lines.append(
            f"{period.period_name} ({ts} UTC)\n"
            f"🏄 {period.spot.name}\n"
            f"{stars}  {period.score}/100 — {cat}\n"
            f"🌊 {step.wave_height_m:.1f}m · {swell_arrow}{swell_dir} · {step.swell_period_s:.0f}s\n"
            f"⚡ {_wave_energy(step.wave_height_m, step.swell_period_s)}  "
            f"💨 {wind_arrow}{wind_dir} {step.wind_speed_kmh:.0f} km/h  "
            f"🌡 {weather}\n"
        )

    if not has_any:
        return f"🔮 No forecast data near {daily.location_label}."
    return "\n".join(lines).strip()


def _location_button_label(location: Any) -> str:
    if location.label:
        return str(location.label)
    if location.city:
        return str(location.city)
    return f"{location.lat:.3f}, {location.lon:.3f}"


def _created_text(subscription_id: int) -> str:
    return f"✅ Subscription #{subscription_id} created!"


# --- static copy ------------------------------------------------------- #
_NEED_START_TEXT: Final = "Please run /start first so I can set up your account."
_ACTIVITY_UNKNOWN_TEXT: Final = "Activity unavailable. Please try again later."
_CANCELLED_TEXT: Final = "Cancelled."
