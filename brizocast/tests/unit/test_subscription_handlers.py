"""Unit tests for the unified subscriptions conversation handler.

The new flow:
- Entry (📋 My subscriptions) → shows list of subscriptions as inline buttons + ➕ Subscribe
- Tap subscription → detail with Forecast / Remove / Back buttons
- ➕ Subscribe → pick a favorite location → subscription created
- Remove → confirm → deleted
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from telegram import InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
)

from brizocast.activities.bootstrap import register_builtin_activities
from brizocast.bot.handlers.subscriptions import (
    CTX_ACTIVITY_IDS,
    CTX_DB_USER_ID,
    _ADD_LOCATION,
    _DETAIL,
    _LIST,
    _NEED_START_TEXT,
    _NO_LOCATION_TEXT,
    _REMOVE_CONFIRM,
    build_subscription_handlers,
)
from brizocast.bot.keyboards.common import ConfirmCallbackData, encode_confirm_callback
from brizocast.core.errors import DomainValidationError, NotFoundError
from brizocast.services.subscription_service import SubscriptionSummary

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
    is_favorite: bool = True


@dataclass
class FakeSubscription:
    id: int = 101


class FakeSubscriptionService:
    def __init__(
        self,
        summaries: list[SubscriptionSummary] | None = None,
        *,
        raise_on_create: Exception | None = None,
    ) -> None:
        self._summaries = summaries or []
        self._raise_on_create = raise_on_create
        self.created: list[dict[str, Any]] = []
        self.removed: list[int] = []

    async def summarize_for_user(self, user_id: int) -> list[SubscriptionSummary]:
        return self._summaries

    async def create(
        self,
        user_id: int,
        activity_id: int,
        location_id: int | None,
        *,
        search_radius_km: float | None = None,
        preset_id: int | None = None,
        notification_mode: str = "immediate",
    ) -> FakeSubscription:
        if self._raise_on_create:
            raise self._raise_on_create
        self.created.append({
            "user_id": user_id,
            "activity_id": activity_id,
            "location_id": location_id,
            "search_radius_km": search_radius_km,
            "preset_id": preset_id,
            "notification_mode": notification_mode,
        })
        return FakeSubscription(id=101)

    async def remove(self, subscription_id: int) -> None:
        self.removed.append(subscription_id)


class FakeLocationService:
    def __init__(self, locations: list[FakeLocation] | None = None) -> None:
        self._locations = locations or []

    async def list_favorites(self, user_id: int) -> list[FakeLocation]:
        return self._locations


# --------------------------------------------------------------------------- #
# telegram fakes
# --------------------------------------------------------------------------- #
@dataclass
class FakeMessage:
    text: str | None = None
    replies: list[tuple[str, Any]] = field(default_factory=list)

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


@dataclass
class FakeTelegramUser:
    id: int = 100
    username: str | None = "testuser"


@dataclass
class FakeUpdate:
    effective_message: FakeMessage | None = None
    callback_query: FakeCallbackQuery | None = None
    effective_user: FakeTelegramUser = field(default_factory=FakeTelegramUser)


class FakeContext:
    def __init__(self) -> None:
        self.user_data: dict[str, Any] = {}
        self.bot_data: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _summary(subscription_id: int = 1) -> SubscriptionSummary:
    return SubscriptionSummary(
        subscription_id=subscription_id,
        activity_key="surf",
        activity_display_name="🏄 Surf",
        location_label="Home",
        location_place="Lisbon, PT",
        search_radius_km=20.0,
        notification_mode="immediate",
    )


class FakeUserService:
    def __init__(self, user_id: int = 7) -> None:
        self._user_id = user_id

    async def get_or_create_user(self, telegram_user_id: int, username: str | None = None) -> Any:
        class _U:
            id: int
        u = _U()
        u.id = self._user_id
        return u


def _ctx(user_id: int | None = 7) -> FakeContext:
    ctx = FakeContext()
    if user_id is not None:
        ctx.user_data[CTX_DB_USER_ID] = user_id
    register_builtin_activities()
    ctx.bot_data[CTX_ACTIVITY_IDS] = {"surf": 1}
    return ctx


def _build(
    sub: FakeSubscriptionService | None = None,
    loc: FakeLocationService | None = None,
) -> Any:
    handlers = build_subscription_handlers(
        sub or FakeSubscriptionService(),  # type: ignore[arg-type]
        loc or FakeLocationService(),  # type: ignore[arg-type]
        FakeUserService(),  # type: ignore[arg-type]
    )
    return handlers[0]  # single unified ConversationHandler


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
async def test_list_shows_subscriptions_and_add_button() -> None:
    sub = FakeSubscriptionService(summaries=[_summary(1)])
    handler = _build(sub)
    ctx = _ctx()
    msg = FakeMessage()
    update = FakeUpdate(effective_message=msg)

    result = await handler.entry_points[0].callback(update, ctx)

    assert result == _LIST
    assert "Your subscriptions" in msg.last_text
    assert isinstance(msg.last_markup, InlineKeyboardMarkup)
    # One subscription button + one "➕ Subscribe" button
    buttons = [btn for row in msg.last_markup.inline_keyboard for btn in row]
    assert len(buttons) == 2
    assert "Subscribe" in buttons[-1].text


async def test_list_empty_still_shows_add_button() -> None:
    handler = _build(FakeSubscriptionService(summaries=[]))
    ctx = _ctx()
    msg = FakeMessage()
    update = FakeUpdate(effective_message=msg)

    result = await handler.entry_points[0].callback(update, ctx)

    assert result == _LIST
    assert "No subscriptions" in msg.last_text


async def test_add_no_location_informs_user() -> None:
    handler = _build(loc=FakeLocationService(locations=[]))
    ctx = _ctx()
    q = FakeCallbackQuery(data="sub:add")

    result = await handler.states[_LIST][1].callback(FakeUpdate(callback_query=q), ctx)

    assert result == _END
    assert _NO_LOCATION_TEXT in q.last_text


async def test_add_happy_path_creates_subscription() -> None:
    sub = FakeSubscriptionService()
    loc = FakeLocationService(locations=[FakeLocation(id=5, label="Home")])
    handler = _build(sub, loc)
    ctx = _ctx()

    # Tap ➕ Subscribe
    q = FakeCallbackQuery(data="sub:add")
    result = await handler.states[_LIST][1].callback(FakeUpdate(callback_query=q), ctx)
    assert result == _ADD_LOCATION

    # Pick location
    q2 = FakeCallbackQuery(data="addloc:5")
    result = await handler.states[_ADD_LOCATION][0].callback(FakeUpdate(callback_query=q2), ctx)
    assert result == _END

    assert len(sub.created) == 1
    assert sub.created[0]["location_id"] == 5
    assert sub.created[0]["notification_mode"] == "immediate"


async def test_detail_shows_actions() -> None:
    sub = FakeSubscriptionService(summaries=[_summary(42)])
    handler = _build(sub)
    ctx = _ctx()

    q = FakeCallbackQuery(data="subdet:42")
    result = await handler.states[_LIST][0].callback(FakeUpdate(callback_query=q), ctx)

    assert result == _DETAIL
    assert "Home" in q.last_text


async def test_remove_from_detail() -> None:
    sub = FakeSubscriptionService(summaries=[_summary(42)])
    handler = _build(sub)
    ctx = _ctx()

    # Tap remove
    q = FakeCallbackQuery(data="subrm:42")
    result = await handler.states[_DETAIL][1].callback(FakeUpdate(callback_query=q), ctx)
    assert result == _REMOVE_CONFIRM

    # Confirm yes
    q2 = FakeCallbackQuery(data=encode_confirm_callback(ConfirmCallbackData(action="remove:42", answer=True)))
    result = await handler.states[_REMOVE_CONFIRM][0].callback(FakeUpdate(callback_query=q2), ctx)
    assert result == _END
    assert 42 in sub.removed
