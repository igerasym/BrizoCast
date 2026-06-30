"""Unit tests for the simplified ``/location`` conversation (task 7.3, Req 2.*).

The entry is now a single step: the user either taps *share my location* (a
shared point) or types a city/place name (a search). Saved spots are reached via
the ``/favorites`` command. These tests drive the thin conversation built by
:func:`brizocast.bot.handlers.location.build_location_handlers` against fakes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)

from brizocast.bot.handlers.location import LocationState, build_location_handlers
from brizocast.bot.handlers.subscriptions import CTX_DB_USER_ID
from brizocast.bot.keyboards.common import ConfirmCallbackData, encode_confirm_callback
from brizocast.bot.keyboards.locations import (
    encode_candidate_callback,
    encode_favorite_delete_callback,
)
from brizocast.core.domain.geo import GeoCandidate
from brizocast.core.errors import ProviderRequestError

pytestmark = pytest.mark.unit

_END = ConversationHandler.END


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
@dataclass
class FakeLocation:
    id: int
    label: str | None = None
    lat: float = 0.0
    lon: float = 0.0
    city: str | None = None
    country: str | None = None
    is_favorite: bool = False


@dataclass
class FakeUser:
    id: int = 7


class FakeUserService:
    def __init__(self, user_id: int = 7) -> None:
        self._user = FakeUser(id=user_id)
        self.created: list[tuple[int, str | None]] = []

    async def get_or_create_user(
        self, telegram_user_id: int, username: str | None = None
    ) -> FakeUser:
        self.created.append((telegram_user_id, username))
        return self._user


class FakeLocationService:
    def __init__(
        self,
        *,
        candidates: list[GeoCandidate] | None = None,
        favorites: list[FakeLocation] | None = None,
        search_error: Exception | None = None,
    ) -> None:
        self._candidates = candidates or []
        self._favorites = favorites or []
        self._search_error = search_error
        self.from_coords: list[tuple[int, float, float]] = []
        self.from_candidate: list[tuple[int, GeoCandidate]] = []
        self.saved: list[int] = []
        self.deleted: list[int] = []
        self.searched: list[str] = []
        self._next_id = 100

    async def create_from_coordinates(
        self, user_id: int, lat: float, lon: float, **_: Any
    ) -> FakeLocation:
        self.from_coords.append((user_id, lat, lon))
        self._next_id += 1
        return FakeLocation(id=self._next_id, lat=lat, lon=lon)

    async def create_from_candidate(
        self, user_id: int, candidate: GeoCandidate, **_: Any
    ) -> FakeLocation:
        self.from_candidate.append((user_id, candidate))
        self._next_id += 1
        return FakeLocation(
            id=self._next_id,
            label=candidate.name,
            lat=candidate.lat,
            lon=candidate.lon,
            city=candidate.city,
            country=candidate.country,
        )

    async def save_favorite(self, location_id: int) -> FakeLocation:
        self.saved.append(location_id)
        return FakeLocation(id=location_id, is_favorite=True)

    async def list_favorites(self, user_id: int) -> list[FakeLocation]:
        return self._favorites

    async def delete_favorite(self, location_id: int) -> None:
        self.deleted.append(location_id)

    async def search(self, query: str, limit: int = 5) -> list[GeoCandidate]:
        self.searched.append(query)
        if self._search_error is not None:
            raise self._search_error
        return self._candidates


@dataclass
class FakeTelegramLocation:
    latitude: float
    longitude: float


@dataclass
class FakeChat:
    """Stub for telegram.Chat used in location tests."""

    async def send_action(self, action: str) -> None:  # noqa: ARG002
        pass


@dataclass
class FakeMessage:
    text: str | None = None
    location: FakeTelegramLocation | None = None
    replies: list[tuple[str, Any]] = field(default_factory=list)
    chat: FakeChat = field(default_factory=FakeChat)

    async def reply_text(self, text: str, reply_markup: Any = None) -> None:
        self.replies.append((text, reply_markup))

    @property
    def last_text(self) -> str:
        return self.replies[-1][0]

    @property
    def last_markup(self) -> Any:
        return self.replies[-1][1]


@dataclass
class FakeCallbackQuery:
    data: str
    answers: int = 0
    edits: list[tuple[str, Any]] = field(default_factory=list)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers += 1

    async def edit_message_text(self, text: str, reply_markup: Any = None) -> None:
        self.edits.append((text, reply_markup))

    @property
    def last_text(self) -> str:
        return self.edits[-1][0]

    @property
    def last_markup(self) -> Any:
        return self.edits[-1][1]


@dataclass
class FakeTelegramUser:
    id: int = 42
    username: str | None = "ana"


@dataclass
class FakeUpdate:
    effective_user: FakeTelegramUser | None = None
    effective_message: FakeMessage | None = None
    callback_query: FakeCallbackQuery | None = None


@dataclass
class FakeContext:
    user_data: dict[Any, Any] = field(default_factory=dict)
    bot_data: dict[Any, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _conversation(
    location_service: FakeLocationService,
    user_service: FakeUserService | None = None,
) -> Any:
    handlers = build_location_handlers(
        location_service,  # type: ignore[arg-type]
        user_service or FakeUserService(),  # type: ignore[arg-type]
    )
    return handlers[0]


def _location_entry_cb(conv: Any) -> Any:
    """The /location entry callback (location_start)."""
    return conv.entry_points[0].callback


def _favorites_entry_cb(conv: Any) -> Any:
    """The /favorites entry callback (show_favorites_cmd)."""
    for handler in conv.entry_points:
        if isinstance(handler, CommandHandler) and "favorites" in handler.commands:
            return handler.callback
    raise AssertionError("no /favorites entry point")


def _entry_handler_cb(conv: Any, callback_name: str) -> Any:
    """Return the ENTRY-state handler whose callback has ``callback_name``."""
    for handler in conv.states[LocationState.ENTRY]:
        cb = getattr(handler, "callback", None)
        if getattr(cb, "__name__", "") == callback_name:
            return cb
    raise AssertionError(f"no ENTRY handler named {callback_name}")


def _state_cb(conv: Any, state: int, cls: type) -> Any:
    for handler in conv.states[state]:
        if isinstance(handler, cls):
            return getattr(handler, "callback")
    raise AssertionError(f"no {cls.__name__} registered for state {state}")


def _candidate(name: str, lat: float, lon: float, **kw: Any) -> GeoCandidate:
    return GeoCandidate(name=name, lat=lat, lon=lon, **kw)


async def _enter(conv: Any, ctx: FakeContext) -> FakeMessage:
    """Run the /location entry point and return the prompt message."""
    message = FakeMessage()
    update = FakeUpdate(effective_user=FakeTelegramUser(), effective_message=message)
    state = await _location_entry_cb(conv)(update, ctx)
    assert state == LocationState.ENTRY
    return message


# --------------------------------------------------------------------------- #
# entry — one step: share button + type-to-search (Req 2.1, 2.2)
# --------------------------------------------------------------------------- #
async def test_location_entry_resolves_user_and_shows_share_button() -> None:
    users = FakeUserService(user_id=7)
    conv = _conversation(FakeLocationService(), users)
    ctx = FakeContext()

    message = await _enter(conv, ctx)

    assert users.created == [(42, "ana")]  # provisioned on first interaction
    assert ctx.user_data[CTX_DB_USER_ID] == 7  # cached for sibling handlers
    # Entry goes straight to a reply keyboard with the share-location button.
    assert isinstance(message.last_markup, ReplyKeyboardMarkup)


# --------------------------------------------------------------------------- #
# shared Telegram location creates a location (Req 2.2) + save offer (Req 2.7)
# --------------------------------------------------------------------------- #
async def test_shared_location_creates_location_and_offers_save() -> None:
    loc = FakeLocationService()
    conv = _conversation(loc)
    ctx = FakeContext()
    await _enter(conv, ctx)

    # Share a Telegram location directly from the entry step (no extra taps).
    shared_cb = _entry_handler_cb(conv, "location_shared")
    msg = FakeMessage(location=FakeTelegramLocation(latitude=38.7, longitude=-9.1))
    state = await shared_cb(FakeUpdate(effective_message=msg), ctx)

    assert state == LocationState.SAVING
    assert loc.from_coords == [(7, 38.7, -9.1)]  # created from shared point (Req 2.2)
    # The last message offers the save-as-favorite confirm keyboard (Req 2.7).
    assert isinstance(msg.last_markup, InlineKeyboardMarkup)


# --------------------------------------------------------------------------- #
# typing a place searches (Req 2.3, 2.4) and a selection creates one (Req 2.5)
# --------------------------------------------------------------------------- #
async def test_typed_search_shows_candidates_then_selection_creates_location() -> None:
    candidates = [
        _candidate("Ericeira", 38.96, -9.41, city="Ericeira", country="PT"),
        _candidate("Erie", 42.13, -80.08, city="Erie", country="US"),
    ]
    loc = FakeLocationService(candidates=candidates)
    conv = _conversation(loc)
    ctx = FakeContext()
    await _enter(conv, ctx)

    # Type a term in the entry step → candidates shown (Req 2.3, 2.4).
    search_cb = _entry_handler_cb(conv, "search_entered")
    msg = FakeMessage(text="Eri")
    assert await search_cb(FakeUpdate(effective_message=msg), ctx) == LocationState.PICKING
    assert loc.searched == ["Eri"]
    assert isinstance(msg.last_markup, InlineKeyboardMarkup)
    assert len(msg.last_markup.inline_keyboard) == 2  # one row per candidate

    # Pick the first candidate → location created from it (Req 2.5).
    pick_cb = _state_cb(conv, LocationState.PICKING, CallbackQueryHandler)
    pick_q = FakeCallbackQuery(data=encode_candidate_callback(0))
    msg2 = FakeMessage()
    assert await pick_cb(
        FakeUpdate(effective_message=msg2, callback_query=pick_q), ctx
    ) == LocationState.SAVING
    assert loc.from_candidate == [(7, candidates[0])]


# --------------------------------------------------------------------------- #
# no-match re-prompts for a new term (Req 2.6)
# --------------------------------------------------------------------------- #
async def test_search_no_match_reprompts() -> None:
    loc = FakeLocationService(candidates=[])
    conv = _conversation(loc)
    ctx = FakeContext()
    await _enter(conv, ctx)

    search_cb = _entry_handler_cb(conv, "search_entered")
    msg = FakeMessage(text="Nowhereville")
    state = await search_cb(FakeUpdate(effective_message=msg), ctx)

    assert state == LocationState.SEARCHING  # stays in search (Req 2.6)
    assert "couldn't find" in msg.last_text.lower()
    assert loc.from_candidate == []


# --------------------------------------------------------------------------- #
# provider failure → temporarily unavailable (Req 2.11)
# --------------------------------------------------------------------------- #
async def test_search_provider_failure_reports_unavailable() -> None:
    loc = FakeLocationService(
        search_error=ProviderRequestError("boom", provider="open_meteo_geocoding")
    )
    conv = _conversation(loc)
    ctx = FakeContext()
    await _enter(conv, ctx)

    search_cb = _entry_handler_cb(conv, "search_entered")
    msg = FakeMessage(text="Lisbon")
    state = await search_cb(FakeUpdate(effective_message=msg), ctx)

    assert state == _END
    assert "temporarily unavailable" in msg.last_text.lower()


# --------------------------------------------------------------------------- #
# save-as-favorite confirm (Req 2.7)
# --------------------------------------------------------------------------- #
async def test_save_favorite_confirm_yes_saves() -> None:
    loc = FakeLocationService()
    conv = _conversation(loc)
    ctx = FakeContext()
    ctx.user_data[CTX_DB_USER_ID] = 7

    save_cb = _state_cb(conv, LocationState.SAVING, CallbackQueryHandler)
    q = FakeCallbackQuery(
        data=encode_confirm_callback(ConfirmCallbackData(answer=True, action="favorite:101"))
    )
    state = await save_cb(FakeUpdate(effective_message=FakeMessage(), callback_query=q), ctx)

    assert state == _END
    assert loc.saved == [101]  # persisted as favorite (Req 2.7)


async def test_save_favorite_confirm_no_does_not_save() -> None:
    loc = FakeLocationService()
    conv = _conversation(loc)
    ctx = FakeContext()

    save_cb = _state_cb(conv, LocationState.SAVING, CallbackQueryHandler)
    q = FakeCallbackQuery(
        data=encode_confirm_callback(ConfirmCallbackData(answer=False, action="favorite:101"))
    )
    state = await save_cb(FakeUpdate(effective_message=FakeMessage(), callback_query=q), ctx)

    assert state == _END
    assert loc.saved == []


# --------------------------------------------------------------------------- #
# favorites via /favorites command (Req 2.9) and deletion (Req 2.10)
# --------------------------------------------------------------------------- #
async def test_favorites_command_lists_label_and_place() -> None:
    favorites = [
        FakeLocation(id=1, label="Home", city="Lisbon", country="PT"),
        FakeLocation(id=2, label="Trip", city="Biarritz", country="FR"),
    ]
    loc = FakeLocationService(favorites=favorites)
    conv = _conversation(loc)
    ctx = FakeContext()

    msg = FakeMessage()
    state = await _favorites_entry_cb(conv)(
        FakeUpdate(effective_user=FakeTelegramUser(), effective_message=msg), ctx
    )

    assert state == LocationState.MANAGING
    text = msg.last_text
    assert "Home" in text and "Lisbon, PT" in text  # label + place (Req 2.9)
    assert "Trip" in text and "Biarritz, FR" in text
    assert isinstance(msg.last_markup, InlineKeyboardMarkup)


async def test_favorites_command_empty_informs_user() -> None:
    loc = FakeLocationService(favorites=[])
    conv = _conversation(loc)
    ctx = FakeContext()

    msg = FakeMessage()
    state = await _favorites_entry_cb(conv)(
        FakeUpdate(effective_user=FakeTelegramUser(), effective_message=msg), ctx
    )

    assert state == _END
    assert "don't have any saved favorites" in msg.last_text.lower()


async def test_favorite_delete_removes_selected() -> None:
    favorites = [FakeLocation(id=5, label="Home", city="Lisbon", country="PT")]
    loc = FakeLocationService(favorites=favorites)
    conv = _conversation(loc)
    ctx = FakeContext()

    delete_cb = _state_cb(conv, LocationState.MANAGING, CallbackQueryHandler)
    q = FakeCallbackQuery(data=encode_favorite_delete_callback(5))
    state = await delete_cb(FakeUpdate(effective_message=FakeMessage(), callback_query=q), ctx)

    assert state == _END
    assert loc.deleted == [5]  # removed the selected favorite (Req 2.10)


async def test_cancel_ends_conversation() -> None:
    conv = _conversation(FakeLocationService())
    ctx = FakeContext()
    msg = FakeMessage()

    cancel_cb = conv.fallbacks[0].callback
    state = await cancel_cb(FakeUpdate(effective_message=msg), ctx)

    assert state == _END
    assert msg.replies  # a cancellation message was sent
