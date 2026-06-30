"""Unit tests for the ``/settings`` conversation handler (task 7.6).

Drives the handler callbacks built by
:func:`brizocast.bot.handlers.settings.build_settings_handlers` against fake
Telegram updates and a fake :class:`SubscriptionService`, verifying the flow
routes each menu action to the right service mutator and persists the change
(Req 10.2, 11.1, 11.3, 11.4, 13.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, cast

import pytest
from telegram import Update
from telegram.ext import BaseHandler, ContextTypes, ConversationHandler

from brizocast.bot.handlers.settings import (
    USER_DB_ID_KEY,
    build_settings_handlers,
)
from brizocast.bot.handlers.settings import (
    _AWAIT_QUIET_HOURS,
    _EDIT_MENU,
    _PICK_SUBSCRIPTION,
)
from brizocast.bot.keyboards.notifications import encode_notification_mode_callback
from brizocast.bot.keyboards.settings import SettingsAction, encode_settings_callback
from brizocast.bot.keyboards.subscriptions import (
    SubscriptionPickPurpose,
    encode_subscription_callback,
)
from brizocast.config.settings import NOTIFICATION_MODE_MORNING_DIGEST
from brizocast.notifications.modes import NotificationMode
from brizocast.services.subscription_service import (
    SubscriptionService,
    SubscriptionSummary,
)

pytestmark = pytest.mark.unit

_SUB_ID = 7


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class FakeService:
    """Records mutator calls; stands in for :class:`SubscriptionService`."""

    summaries: list[SubscriptionSummary] = field(default_factory=list)
    mode_calls: list[tuple[int, str]] = field(default_factory=list)
    quiet_calls: list[tuple[int, time | None, time | None]] = field(default_factory=list)
    mute_calls: list[tuple[int, bool]] = field(default_factory=list)
    snooze_calls: list[tuple[int, datetime | None]] = field(default_factory=list)

    async def summarize_for_user(self, user_id: int) -> list[SubscriptionSummary]:
        return self.summaries

    async def set_notification_mode(self, subscription_id: int, mode: str) -> None:
        self.mode_calls.append((subscription_id, mode))

    async def set_quiet_hours(
        self, subscription_id: int, start: time | None, end: time | None
    ) -> None:
        self.quiet_calls.append((subscription_id, start, end))

    async def set_muted(self, subscription_id: int, muted: bool) -> None:
        self.mute_calls.append((subscription_id, muted))

    async def snooze(self, subscription_id: int, until: datetime | None) -> None:
        self.snooze_calls.append((subscription_id, until))


@dataclass
class FakeMessage:
    """Captures ``reply_text`` calls."""

    text: str | None = None
    replies: list[str] = field(default_factory=list)

    async def reply_text(self, text: str, reply_markup: object | None = None) -> None:
        self.replies.append(text)


@dataclass
class FakeQuery:
    """Captures ``answer`` / ``edit_message_text`` for a callback tap."""

    data: str | None
    answered: bool = False
    edits: list[str] = field(default_factory=list)

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(
        self, text: str, reply_markup: object | None = None
    ) -> None:
        self.edits.append(text)


@dataclass
class FakeUpdate:
    """Minimal stand-in carrying just what the handlers read."""

    effective_message: FakeMessage | None = None
    callback_query: FakeQuery | None = None


@dataclass
class FakeContext:
    """Minimal stand-in exposing ``user_data``."""

    user_data: dict[Any, Any] = field(default_factory=dict)


def _summary() -> SubscriptionSummary:
    return SubscriptionSummary(
        subscription_id=_SUB_ID,
        activity_key="surf",
        activity_display_name="🏄 Surf",
        location_label="Home",
        location_place="Lisbon, Portugal",
        search_radius_km=30.0,
        notification_mode=NOTIFICATION_MODE_MORNING_DIGEST,
    )


def _build(service: FakeService) -> ConversationHandler[ContextTypes.DEFAULT_TYPE]:
    handlers = build_settings_handlers(cast(SubscriptionService, service))
    return cast(
        "ConversationHandler[ContextTypes.DEFAULT_TYPE]",
        handlers[0],
    )


def _callback(
    handler: BaseHandler[Update, ContextTypes.DEFAULT_TYPE, object],
) -> Any:
    return handler.callback


async def _run(cb: Any, update: FakeUpdate, context: FakeContext) -> int:
    result = await cb(
        cast(Update, update), cast("ContextTypes.DEFAULT_TYPE", context)
    )
    return cast(int, result)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def test_settings_without_account_prompts_start() -> None:
    """No resolved user → asks the user to /start (Req 13.5)."""
    service = FakeService(summaries=[_summary()])
    conv = _build(service)
    message = FakeMessage()
    state = await _run(
        _callback(conv.entry_points[0]),
        FakeUpdate(effective_message=message),
        FakeContext(),
    )
    assert state == ConversationHandler.END
    assert any("/start" in r for r in message.replies)


async def test_settings_without_subscriptions_prompts_add() -> None:
    """A resolved user with no subscriptions is told to /add."""
    service = FakeService(summaries=[])
    conv = _build(service)
    message = FakeMessage()
    state = await _run(
        _callback(conv.entry_points[0]),
        FakeUpdate(effective_message=message),
        FakeContext(user_data={USER_DB_ID_KEY: 1}),
    )
    assert state == ConversationHandler.END
    assert any("/add" in r for r in message.replies)


async def test_settings_presents_pick() -> None:
    """A resolved user with subscriptions advances to the pick state."""
    service = FakeService(summaries=[_summary()])
    conv = _build(service)
    message = FakeMessage()
    state = await _run(
        _callback(conv.entry_points[0]),
        FakeUpdate(effective_message=message),
        FakeContext(user_data={USER_DB_ID_KEY: 1}),
    )
    assert state == _PICK_SUBSCRIPTION


# --------------------------------------------------------------------------- #
# Subscription pick → menu
# --------------------------------------------------------------------------- #


async def test_pick_stores_subscription_and_opens_menu() -> None:
    """Picking a subscription stores its id and shows the menu."""
    service = FakeService(summaries=[_summary()])
    conv = _build(service)
    context = FakeContext(user_data={USER_DB_ID_KEY: 1})
    data = encode_subscription_callback(SubscriptionPickPurpose.SETTINGS, _SUB_ID)
    query = FakeQuery(data=data)

    state = await _run(
        _callback(conv.states[_PICK_SUBSCRIPTION][0]),
        FakeUpdate(callback_query=query),
        context,
    )

    assert state == _EDIT_MENU
    assert query.answered
    assert context.user_data["settings_subscription_id"] == _SUB_ID


# --------------------------------------------------------------------------- #
# Menu actions → mutators
# --------------------------------------------------------------------------- #


def _menu_action_handler(
    conv: ConversationHandler[ContextTypes.DEFAULT_TYPE],
) -> Any:
    return _callback(conv.states[_EDIT_MENU][0])


def _menu_context() -> FakeContext:
    return FakeContext(user_data={USER_DB_ID_KEY: 1, "settings_subscription_id": _SUB_ID})


async def test_mute_action_persists() -> None:
    """The Mute button persists muted=True (Req 11.3)."""
    service = FakeService()
    conv = _build(service)
    query = FakeQuery(data=encode_settings_callback(SettingsAction.MUTE, "1"))

    state = await _run(
        _menu_action_handler(conv), FakeUpdate(callback_query=query), _menu_context()
    )

    assert state == _EDIT_MENU
    assert service.mute_calls == [(_SUB_ID, True)]


async def test_unmute_action_persists() -> None:
    """The Unmute button persists muted=False (Req 11.3)."""
    service = FakeService()
    conv = _build(service)
    query = FakeQuery(data=encode_settings_callback(SettingsAction.MUTE, "0"))

    await _run(
        _menu_action_handler(conv), FakeUpdate(callback_query=query), _menu_context()
    )

    assert service.mute_calls == [(_SUB_ID, False)]


async def test_snooze_duration_persists_future_deadline() -> None:
    """A snooze duration persists a non-None future deadline (Req 11.4)."""
    service = FakeService()
    conv = _build(service)
    query = FakeQuery(data=encode_settings_callback(SettingsAction.SNOOZE, "180"))

    await _run(
        _menu_action_handler(conv), FakeUpdate(callback_query=query), _menu_context()
    )

    assert len(service.snooze_calls) == 1
    sub_id, until = service.snooze_calls[0]
    assert sub_id == _SUB_ID
    assert until is not None


async def test_snooze_clear_persists_none() -> None:
    """The clear-snooze button persists None (Req 11.4)."""
    service = FakeService()
    conv = _build(service)
    query = FakeQuery(data=encode_settings_callback(SettingsAction.SNOOZE, "0"))

    await _run(
        _menu_action_handler(conv), FakeUpdate(callback_query=query), _menu_context()
    )

    assert service.snooze_calls == [(_SUB_ID, None)]


async def test_quiet_clear_persists_none_window() -> None:
    """The clear-quiet-hours button persists a None/None window (Req 11.1)."""
    service = FakeService()
    conv = _build(service)
    query = FakeQuery(data=encode_settings_callback(SettingsAction.QUIET_CLEAR))

    await _run(
        _menu_action_handler(conv), FakeUpdate(callback_query=query), _menu_context()
    )

    assert service.quiet_calls == [(_SUB_ID, None, None)]


async def test_quiet_action_awaits_text_input() -> None:
    """Choosing quiet hours advances to the text-entry state (Req 11.1)."""
    service = FakeService()
    conv = _build(service)
    query = FakeQuery(data=encode_settings_callback(SettingsAction.QUIET))

    state = await _run(
        _menu_action_handler(conv), FakeUpdate(callback_query=query), _menu_context()
    )

    assert state == _AWAIT_QUIET_HOURS
    assert service.quiet_calls == []


async def test_menu_without_subscription_ends() -> None:
    """A menu tap with no stored subscription ends the conversation."""
    service = FakeService()
    conv = _build(service)
    query = FakeQuery(data=encode_settings_callback(SettingsAction.MUTE, "1"))

    state = await _run(
        _menu_action_handler(conv),
        FakeUpdate(callback_query=query),
        FakeContext(user_data={USER_DB_ID_KEY: 1}),
    )

    assert state == ConversationHandler.END
    assert service.mute_calls == []


# --------------------------------------------------------------------------- #
# Notification mode
# --------------------------------------------------------------------------- #


async def test_mode_chosen_persists() -> None:
    """Choosing a notification mode persists it (Req 10.2)."""
    service = FakeService()
    conv = _build(service)
    mode_handler = _callback(conv.states[_EDIT_MENU][1])
    query = FakeQuery(
        data=encode_notification_mode_callback(NotificationMode.MORNING_DIGEST)
    )

    state = await _run(mode_handler, FakeUpdate(callback_query=query), _menu_context())

    assert state == _EDIT_MENU
    assert service.mode_calls == [(_SUB_ID, NotificationMode.MORNING_DIGEST.value)]


# --------------------------------------------------------------------------- #
# Quiet-hours text entry
# --------------------------------------------------------------------------- #


def _quiet_handler(conv: ConversationHandler[ContextTypes.DEFAULT_TYPE]) -> Any:
    return _callback(conv.states[_AWAIT_QUIET_HOURS][0])


async def test_quiet_hours_valid_input_persists() -> None:
    """A valid 'HH:MM-HH:MM' reply persists the window (Req 11.1)."""
    service = FakeService()
    conv = _build(service)
    message = FakeMessage(text="22:00-07:00")

    state = await _run(
        _quiet_handler(conv), FakeUpdate(effective_message=message), _menu_context()
    )

    assert state == _EDIT_MENU
    assert service.quiet_calls == [(_SUB_ID, time(22, 0), time(7, 0))]


async def test_quiet_hours_invalid_input_reprompts() -> None:
    """An unparseable reply re-prompts and persists nothing (Req 11.1)."""
    service = FakeService()
    conv = _build(service)
    message = FakeMessage(text="not a time")

    state = await _run(
        _quiet_handler(conv), FakeUpdate(effective_message=message), _menu_context()
    )

    assert state == _AWAIT_QUIET_HOURS
    assert service.quiet_calls == []
    assert message.replies  # a guidance message was sent
