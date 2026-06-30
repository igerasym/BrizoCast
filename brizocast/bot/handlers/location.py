"""Location command and conversation handlers (thin Telegram adapters).

Implements the bot's ``/location`` surface (task 7.3) as a thin
``python-telegram-bot`` :class:`~telegram.ext.ConversationHandler` that parses
input, calls a service, and formats a reply — no business rules live here. The
location use cases belong to
:class:`~brizocast.services.location_service.LocationService` (creating
locations, managing favorites, and the geocoding-search passthrough) and to
:class:`~brizocast.services.user_service.UserService` (resolving the Telegram
user to its internal database id).

:func:`build_location_handlers` is the dependency-injection entry point: the
application composition root (task 11.1) passes the live services and gets back
the handlers to register on the ``Application``; this module never touches the
``Application`` itself.

Flow (Req 2.1-2.11)
-------------------
``/location`` opens an inline menu offering four ways to set a location
(Req 2.1): share a Telegram location, search by city, search by place name, or
view saved favorites.

* **Share** (Req 2.2): the user shares a Telegram location; the handler creates
  a :class:`~brizocast.models.location.Location` from the shared latitude and
  longitude via :meth:`LocationService.create_from_coordinates`, then offers to
  save it as a favorite (Req 2.7).
* **Search by city / place** (Req 2.3-2.6): the user types a search term; the
  handler asks :meth:`LocationService.search` for candidates. With one or more
  candidates it shows an inline keyboard for selection (Req 2.4) and, on a tap,
  creates a location from the chosen candidate's coordinates, city, and country
  (Req 2.5), then offers to save it as a favorite. With no candidates it tells
  the user nothing matched and re-prompts for a new term (Req 2.6). If the
  geocoding request fails (:class:`ProviderRequestError`), it tells the user the
  search is temporarily unavailable (Req 2.11) — the service has already logged
  the failure.
* **Favorites** (Req 2.9, 2.10): lists each saved favorite with its label and
  place name (Req 2.9) and a delete keyboard; tapping one removes exactly that
  favorite (Req 2.10).

Cross-handler context contract
------------------------------
On entry the handler resolves the Telegram user to its internal database user id
through :meth:`UserService.get_or_create_user` (provisioning on first
interaction, Req 1.7) and caches it under
``context.user_data[`` :data:`~brizocast.bot.handlers.subscriptions.CTX_DB_USER_ID`
``]`` — the same key the subscription handlers read — so later commands reuse the
resolved id.

Requirements covered: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.9, 2.10, 2.11.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Final, cast

from telegram import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
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

from brizocast.bot.handlers.subscriptions import CTX_DB_USER_ID
from brizocast.bot.keyboards.callbacks import (
    CALLBACK_VERSION,
    NAMESPACE_LOCATION,
    NAMESPACE_LOCATION_CANDIDATE,
    NAMESPACE_LOCATION_FAVORITE,
)
from brizocast.bot.keyboards.locations import (
    LocationOption,
    build_candidate_keyboard,
    build_favorites_delete_keyboard,
    build_location_options_keyboard,
    parse_candidate_callback,
    parse_favorite_delete_callback,
    parse_location_callback,
)
from brizocast.bot.keyboards.menu import (
    any_menu_label_filter,
    build_main_menu_keyboard,
    menu_filter,
)
from brizocast.core.domain.geo import GeoCandidate, GeoPoint
from brizocast.core.errors import ProviderRequestError
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.models.location import Location
from brizocast.models.subscription import DEFAULT_SEARCH_RADIUS_KM
from brizocast.services.location_service import LocationService
from brizocast.services.spot_discovery_service import SpotDiscoveryService
from brizocast.services.spot_ingestion_service import SpotIngestionService
from brizocast.services.user_service import UserService

__all__ = ["LocationState", "build_location_handlers"]

# Internal ``user_data`` key holding the candidates last offered for selection,
# so a candidate-pick callback (which carries only a list index) can resolve the
# full ``GeoCandidate`` without re-querying the provider.
_CTX_CANDIDATES: Final = "location_search_candidates"

# Maximum number of nearby surf spots listed in the post-share preview.
_MAX_NEARBY_PREVIEW: Final = 5

# --- callback routing patterns (built from the shared scheme) ---------- #
_OPTION_PATTERN: Final = rf"^{NAMESPACE_LOCATION}:{CALLBACK_VERSION}:"
_CANDIDATE_PATTERN: Final = rf"^{NAMESPACE_LOCATION_CANDIDATE}:{CALLBACK_VERSION}:"
_FAVORITE_PATTERN: Final = rf"^{NAMESPACE_LOCATION_FAVORITE}:{CALLBACK_VERSION}:"

_END: Final = ConversationHandler.END


class LocationState(IntEnum):
    """States of the ``/location`` conversation.

    ``ENTRY`` is a combined step: the user either taps *share my location*
    (a shared point) or types a city/place name (a search).
    ``SEARCHING`` re-prompts after a no-match; ``PICKING`` waits for a
    geocoding-candidate tap; ``MANAGING`` waits for a favorite-delete tap.
    """

    ENTRY = 0
    SEARCHING = 1
    PICKING = 2
    MANAGING = 3


def build_location_handlers(
    location_service: LocationService,
    user_service: UserService,
    *,
    spot_discovery: SpotDiscoveryService | None = None,
    spot_ingestion: SpotIngestionService | None = None,
    ingest_radius_km: float = 50.0,
    logger: BoundLogger | None = None,
) -> list[BaseHandler[Any, ContextTypes.DEFAULT_TYPE, Any]]:
    """Build the ``/location`` handlers bound to the given services.

    Services are captured by the closures below (dependency injection) so the
    handlers stay thin and the wiring lives at the composition root. The
    returned handler is ready to register on a ``python-telegram-bot``
    ``Application`` (done in task 11.1); this function does not touch it.

    :param location_service: Creates locations and manages favorites, and passes
        free-text searches through to the geocoding provider.
    :param user_service: Resolves/creates the internal database user for the
        Telegram user on first interaction (Req 1.7).
    :param spot_discovery: Optional spot-discovery service; when provided, a
        newly-set location shows a short preview of the surf spots within the
        default search radius so the user immediately sees what they'd monitor.
    :param spot_ingestion: Optional ingestion service; when provided, setting a
        location first imports nearby named spots from the spot catalogue
        (Surfline) into our dataset, so the preview reflects them. Failures are
        swallowed by the service and never block the flow.
    :param ingest_radius_km: Area radius (km) imported from the catalogue when a
        location is set (wider than the preview radius to populate the region).
    :param logger: Optional bound logger; one is created when omitted.
    :returns: The handlers to register, in registration order.
    """

    log = logger or get_logger(__name__)

    async def _restore_menu(update: Update) -> None:
        """Send a short message that re-shows the persistent main-menu keyboard.

        Used at terminal points reached via inline taps (where we cannot attach
        a reply keyboard to the edited message) so the bottom navigation is
        always present when the location flow ends.
        """
        message = update.effective_message
        if message is not None:
            await message.reply_text(
                _BACK_TO_MENU_TEXT, reply_markup=build_main_menu_keyboard()
            )

    async def _import_and_preview(location: Location) -> str:
        """Import nearby catalogue spots (if enabled) then build the preview text.

        Ingestion runs first so the preview, read from our own dataset, includes
        any freshly-imported spots. Ingestion is graceful (never raises), so a
        catalogue outage simply yields the existing-dataset preview.
        """
        if spot_ingestion is not None:
            await spot_ingestion.ingest_near(
                location.lat, location.lon, ingest_radius_km
            )
        return _nearby_spots_text(location)

    def _nearby_spots_text(location: Location) -> str:
        """Build a short "spots within the default radius" preview for a location.

        Returns an empty string when no discovery service is wired (e.g. in unit
        tests), so the caller can append it unconditionally.
        """
        if spot_discovery is None:
            return ""
        result = spot_discovery.discover(
            GeoPoint(lat=location.lat, lon=location.lon),
            DEFAULT_SEARCH_RADIUS_KM,
        )
        radius = f"{DEFAULT_SEARCH_RADIUS_KM:g}"
        if not result.has_nearby_spots:
            return (
                f"\n\nNo surf spots within {radius} km. When you /add a "
                "subscription you can widen the search radius."
            )
        shown = result.spots[:_MAX_NEARBY_PREVIEW]
        lines = "\n".join(f"• {spot.name}" for spot in shown)
        extra = len(result.spots) - len(shown)
        more = f"\n…and {extra} more" if extra > 0 else ""
        return (
            f"\n\n🌊 Surf spots within {radius} km:\n{lines}{more}"
        )

    # ------------------------------------------------------------------- #
    # entry — share button + saved locations list (Req 2.1, 2.2, 2.9)
    # ------------------------------------------------------------------- #
    async def location_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        tg_user = update.effective_user
        message = update.effective_message
        if tg_user is None or message is None:  # pragma: no cover - defensive
            return _END

        user = await user_service.get_or_create_user(tg_user.id, tg_user.username)
        _user_data(context)[CTX_DB_USER_ID] = user.id

        # Show share button + search prompt
        await message.reply_text(
            _ENTRY_PROMPT, reply_markup=_share_location_keyboard()
        )

        # Also show saved locations (if any) inline so the user can delete them
        favorites = await location_service.list_favorites(user.id)
        if favorites:
            text = _format_favorites_list(favorites)
            keyboard = build_favorites_delete_keyboard(
                [(fav.id, _favorite_label(fav)) for fav in favorites]
            )
            await message.reply_text(text, reply_markup=keyboard)
            return LocationState.MANAGING

        return LocationState.ENTRY

    # ------------------------------------------------------------------- #
    # shared Telegram location → create from coordinates (Req 2.2)
    # ------------------------------------------------------------------- #
    async def location_shared(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        message = update.effective_message
        assert message is not None and message.location is not None
        user_id = _require_user_id(context)

        # Show a "processing" indicator while ingestion + reverse-geocoding run.
        await message.chat.send_action("typing")

        location = await location_service.create_from_coordinates(
            user_id,
            message.location.latitude,
            message.location.longitude,
            is_favorite=True,
        )
        log.bind(location_id=location.id).info("created location from shared point")
        nearby = await _import_and_preview(location)
        await message.reply_text(
            _created_text(location) + nearby,
            reply_markup=build_main_menu_keyboard(),
        )
        return _END

    # ------------------------------------------------------------------- #
    # search term entered → geocode (Req 2.3, 2.4, 2.6, 2.11)
    # ------------------------------------------------------------------- #
    async def search_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        message = update.effective_message
        query_text = message.text.strip() if message is not None and message.text else ""
        if message is None:  # pragma: no cover - filters guarantee a message
            return LocationState.SEARCHING
        if not query_text:
            await message.reply_text(_EMPTY_QUERY_TEXT)
            return LocationState.SEARCHING

        await message.chat.send_action("typing")

        try:
            candidates = await location_service.search(query_text)
        except ProviderRequestError:
            # The service has already logged the failure; tell the user the
            # search is temporarily unavailable (Req 2.11) and end.
            await message.reply_text(
                _UNAVAILABLE_TEXT, reply_markup=build_main_menu_keyboard()
            )
            return _END

        if not candidates:
            # Nothing matched — ask for a new term and stay in search (Req 2.6).
            await message.reply_text(_NO_MATCH_TEXT)
            return LocationState.SEARCHING

        # Stash the candidates so the index-only callback can resolve them.
        _user_data(context)[_CTX_CANDIDATES] = list(candidates)
        await message.reply_text(
            _CANDIDATE_PROMPT, reply_markup=build_candidate_keyboard(candidates)
        )
        return LocationState.PICKING

    # ------------------------------------------------------------------- #
    # candidate chosen → create from candidate (Req 2.5)
    # ------------------------------------------------------------------- #
    async def candidate_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        assert query is not None and query.data is not None
        await query.answer()

        index = parse_candidate_callback(query.data)
        candidate = _stored_candidate(context, index)
        if candidate is None:
            # The stash expired (e.g. a stale tap); ask the user to search again.
            await query.edit_message_text(_CANDIDATE_EXPIRED_TEXT)
            await _restore_menu(update)
            return _END

        user_id = _require_user_id(context)
        location = await location_service.create_from_candidate(user_id, candidate, is_favorite=True)
        _user_data(context).pop(_CTX_CANDIDATES, None)
        log.bind(location_id=location.id).info("created location from candidate")

        nearby = await _import_and_preview(location)
        await query.edit_message_text(
            _created_text(location) + nearby,
        )
        await _restore_menu(update)
        return _END

    async def favorite_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        assert query is not None and query.data is not None
        await query.answer()

        location_id = parse_favorite_delete_callback(query.data)
        await location_service.delete_favorite(location_id)
        log.bind(location_id=location_id).info("deleted favorite location")
        await query.edit_message_text(_deleted_text(location_id))
        await _restore_menu(update)
        return _END

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _user_data(context).pop(_CTX_CANDIDATES, None)
        message = update.effective_message
        if message is not None:
            await message.reply_text(
                _CANCELLED_TEXT, reply_markup=build_main_menu_keyboard()
            )
        return _END

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("location", location_start),
        ],
        states={
            LocationState.ENTRY: [
                # One step: a shared point creates a location; typed text (that
                # is not a menu-button label) is treated as a place search.
                MessageHandler(filters.LOCATION, location_shared),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~any_menu_label_filter(),
                    search_entered,
                ),
            ],
            LocationState.SEARCHING: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~any_menu_label_filter(),
                    search_entered,
                ),
            ],
            LocationState.PICKING: [
                CallbackQueryHandler(candidate_chosen, pattern=_CANDIDATE_PATTERN),
            ],
            LocationState.MANAGING: [
                CallbackQueryHandler(favorite_delete, pattern=_FAVORITE_PATTERN),
                # Allow sharing / searching a new location while managing saved ones.
                MessageHandler(filters.LOCATION, location_shared),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~any_menu_label_filter(),
                    search_entered,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="location",
        allow_reentry=True,
        persistent=False,
    )

    return [conversation]


# ----------------------------------------------------------------------- #
# small context helpers
# ----------------------------------------------------------------------- #
def _user_data(context: ContextTypes.DEFAULT_TYPE) -> dict[Any, Any]:
    """Return the per-user data dict, raising if the context has none."""

    data = context.user_data
    if data is None:  # pragma: no cover - PTB always provides it for updates
        raise RuntimeError("handler invoked without user_data")
    return data


def _require_user_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return the cached internal database user id resolved at conversation entry."""

    raw = _user_data(context).get(CTX_DB_USER_ID)
    if not isinstance(raw, int):  # pragma: no cover - entry always sets it
        raise RuntimeError("location conversation entered without a resolved user id")
    return raw


def _stored_candidate(
    context: ContextTypes.DEFAULT_TYPE, index: int
) -> GeoCandidate | None:
    """Return the stashed candidate at ``index``, or ``None`` if unavailable."""

    raw = _user_data(context).get(_CTX_CANDIDATES)
    if not isinstance(raw, list) or not 0 <= index < len(raw):
        return None
    return cast("GeoCandidate", raw[index])


# ----------------------------------------------------------------------- #
# pure keyboard / label / text helpers
# ----------------------------------------------------------------------- #
def _share_location_keyboard() -> ReplyKeyboardMarkup:
    """Build the reply keyboard with a native "share my location" button."""

    button = KeyboardButton(_SHARE_BUTTON_LABEL, request_location=True)
    return ReplyKeyboardMarkup(
        [[button]], resize_keyboard=True, one_time_keyboard=True
    )


def _place_name(location: Location) -> str:
    """Render a location's place name from its city/country, with a coord fallback."""

    place = ", ".join(part for part in (location.city, location.country) if part)
    return place or f"{location.lat:.3f}, {location.lon:.3f}"


def _favorite_label(location: Location) -> str:
    """One-line label for a favorite (its label, else its place name)."""

    if location.label:
        return str(location.label)
    return _place_name(location)


def _format_favorites_list(favorites: list[Location]) -> str:
    """Render the favorites listing, one line per favorite (Req 2.9).

    Each line shows the favorite's label and place name. Tapping the matching
    delete button removes it (Req 2.10).
    """

    lines = [_FAVORITES_HEADER]
    lines.extend(f"⭐ {_favorite_label(fav)} — {_place_name(fav)}" for fav in favorites)
    lines.append(_FAVORITES_DELETE_HINT)
    return "\n".join(lines)


def _created_text(location: Location) -> str:
    return f"📍 Location set: {_favorite_label(location)} ({_place_name(location)})."


def _search_prompt(option: LocationOption) -> str:
    noun = "city" if option is LocationOption.SEARCH_CITY else "place"
    return f"Type the {noun} name you want to search for."


def _deleted_text(location_id: int) -> str:
    return f"🗑️ Removed favorite #{location_id}."


# --- static copy ------------------------------------------------------- #
_ENTRY_PROMPT: Final = (
    "📍 Tap the button below to share your location, or type a city or "
    "place name to search.\n\nYour saved locations are listed at the bottom — "
    "tap one to delete it."
)
_BACK_TO_MENU_TEXT: Final = "Done. What next?"
_MENU_PROMPT: Final = "How would you like to set your location?"
_SHARE_PROMPT: Final = "Tap the button below to share your current location."
_SHARE_BUTTON_LABEL: Final = "📍 Share my location"
_CANDIDATE_PROMPT: Final = "I found these places — pick the right one:"
_CANDIDATE_EXPIRED_TEXT: Final = (
    "That search expired. Send /location to start again."
)
_NO_MATCH_TEXT: Final = (
    "I couldn't find a matching place. Try a different search term."
)
_EMPTY_QUERY_TEXT: Final = "Please type a place or city name to search for."
_UNAVAILABLE_TEXT: Final = (
    "Location search is temporarily unavailable. Please try again later."
)
_SAVE_PROMPT: Final = ""  # unused — locations now auto-saved
_NO_FAVORITES_TEXT: Final = (
    "You don't have any saved favorites yet. Set a location and save it to start."
)
_FAVORITES_HEADER: Final = "⭐ Your saved favorites:"
_FAVORITES_DELETE_HINT: Final = "Tap one to delete it."
_CANCELLED_TEXT: Final = "Location setup cancelled."
