"""Unit tests for the onboarding conversation handler (task 7.2, Req 1.1-1.7).

Exercises the four onboarding branches against fakes for the ``UserService`` and
the telegram ``Update``/``Context`` objects:

* new user → activity-selection prompt (Req 1.1, 1.2, 1.3, 1.7),
* unavailable activity → re-prompt and stay in selection (Req 1.4),
* Surf → persist activity and advance to location setup (Req 1.5),
* already-onboarded user → main menu (Req 1.6).

The handler callbacks are pulled off the assembled ``ConversationHandler`` so
the test drives exactly what the framework would dispatch.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from telegram import InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from brizocast.activities.base import Activity
from brizocast.activities.registry import ActivityRegistry
from brizocast.activities.bootstrap import register_builtin_activities
from brizocast.bot.conversations.onboarding import (
    OnboardingState,
    build_onboarding_conversation,
)
from brizocast.bot.formatters.commands import (
    main_menu_text,
    onboarding_welcome_text,
)
from brizocast.bot.keyboards.activities import encode_activity_callback
from brizocast.core.domain.conditions import ConditionsModel
from brizocast.core.ports.scorer import Scorer

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
@dataclass
class _FakeUser:
    """Stand-in for the persisted ``User`` row the service returns."""

    telegram_user_id: int
    username: str | None = None
    onboarded: bool = False
    selected_activity_key: str | None = None


class _FakeUserService:
    """Records calls and returns a configurable user (no DB)."""

    def __init__(self, user: _FakeUser) -> None:
        self._user = user
        self.created: list[tuple[int, str | None]] = []
        self.selected: list[tuple[int, str]] = []

    async def get_or_create_user(
        self, telegram_user_id: int, username: str | None = None
    ) -> _FakeUser:
        self.created.append((telegram_user_id, username))
        return self._user

    async def set_selected_activity(
        self, telegram_user_id: int, activity_key: str
    ) -> _FakeUser:
        self.selected.append((telegram_user_id, activity_key))
        self._user.selected_activity_key = activity_key
        return self._user


@dataclass
class _SentMessage:
    text: str
    reply_markup: Any = None


@dataclass
class _FakeMessage:
    sent: list[_SentMessage] = field(default_factory=list)

    async def reply_text(self, text: str, reply_markup: Any = None) -> None:
        self.sent.append(_SentMessage(text=text, reply_markup=reply_markup))


@dataclass
class _FakeTelegramUser:
    id: int
    username: str | None = None


@dataclass
class _FakeCallbackQuery:
    data: str
    message: _FakeMessage
    answered: int = 0

    async def answer(self) -> None:
        self.answered += 1


@dataclass
class _FakeUpdate:
    effective_user: _FakeTelegramUser | None
    effective_message: _FakeMessage | None
    callback_query: _FakeCallbackQuery | None = None


class _FakeContext:
    def __init__(self) -> None:
        self.user_data: dict[Any, Any] = {}


class _FakeUnavailableActivity(Activity[Any]):
    """A registered-but-unavailable activity for the re-prompt branch."""

    key = "snowboard"
    display_name = "🏂 Snowboard"
    available_in_mvp = False

    def scorer(self) -> Scorer[Any]:  # pragma: no cover - not invoked
        raise NotImplementedError

    def conditions_schema(self) -> type[ConditionsModel]:  # pragma: no cover
        raise NotImplementedError

    def default_forecast_provider_key(self) -> str:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    """Snapshot/restore the process-global registry around each test."""

    snapshot = dict(ActivityRegistry._items)
    ActivityRegistry._items.clear()
    register_builtin_activities()
    ActivityRegistry.register(_FakeUnavailableActivity())
    try:
        yield
    finally:
        ActivityRegistry._items.clear()
        ActivityRegistry._items.update(snapshot)


def _start_callback(service: _FakeUserService) -> Any:
    conv = build_onboarding_conversation(service)  # type: ignore[arg-type]
    return conv.entry_points[0].callback


def _select_callback(service: _FakeUserService) -> Any:
    conv = build_onboarding_conversation(service)  # type: ignore[arg-type]
    return conv.states[OnboardingState.SELECT_ACTIVITY][0].callback


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_new_user_is_provisioned_and_shown_activity_prompt() -> None:
    user = _FakeUser(telegram_user_id=42, username="ana", onboarded=False)
    service = _FakeUserService(user)
    message = _FakeMessage()
    update = _FakeUpdate(_FakeTelegramUser(42, "ana"), message)

    state = await _start_callback(service)(update, _FakeContext())

    assert state == OnboardingState.SELECT_ACTIVITY
    assert service.created == [(42, "ana")]  # user created on first interaction
    assert len(message.sent) == 1
    sent = message.sent[0]
    assert sent.text == onboarding_welcome_text()
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    # Surf is offered and selectable; the unavailable sport is marked locked.
    labels = [b.text for row in sent.reply_markup.inline_keyboard for b in row]
    assert any("Surf" in label and "🔒" not in label for label in labels)
    assert any("🔒" in label for label in labels)


async def test_already_onboarded_user_sees_main_menu() -> None:
    user = _FakeUser(telegram_user_id=7, onboarded=True)
    service = _FakeUserService(user)
    message = _FakeMessage()
    update = _FakeUpdate(_FakeTelegramUser(7), message)

    state = await _start_callback(service)(update, _FakeContext())

    assert state == ConversationHandler.END
    assert [m.text for m in message.sent] == [main_menu_text()]
    assert service.selected == []  # no onboarding work for a returning user


async def test_unavailable_activity_reprompts_and_stays_in_selection() -> None:
    user = _FakeUser(telegram_user_id=1, onboarded=False)
    service = _FakeUserService(user)
    message = _FakeMessage()
    query = _FakeCallbackQuery(
        data=encode_activity_callback("snowboard", available=False),
        message=message,
    )
    update = _FakeUpdate(_FakeTelegramUser(1), message, callback_query=query)

    state = await _select_callback(service)(update, _FakeContext())

    assert state == OnboardingState.SELECT_ACTIVITY  # stays in selection (Req 1.4)
    assert query.answered == 1
    assert service.selected == []  # nothing persisted for an unavailable choice
    assert len(message.sent) == 1
    assert "Snowboard" in message.sent[0].text
    assert "supported" in message.sent[0].text.lower()


async def test_surf_persists_activity_and_advances() -> None:
    user = _FakeUser(telegram_user_id=99, onboarded=False)
    service = _FakeUserService(user)
    message = _FakeMessage()
    query = _FakeCallbackQuery(
        data=encode_activity_callback("surf", available=True),
        message=message,
    )
    context = _FakeContext()
    update = _FakeUpdate(_FakeTelegramUser(99), message, callback_query=query)

    state = await _select_callback(service)(update, context)

    assert state == ConversationHandler.END  # handoff to /location ends the convo
    assert service.selected == [(99, "surf")]  # activity persisted (Req 1.5)
    assert context.user_data["onboarding_activity_key"] == "surf"
    assert len(message.sent) == 1
    assert "/location" in message.sent[0].text
