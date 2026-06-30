"""Integration tests for the ``/presets`` handler and custom-conditions flow (task 7.5).

Drive the thin handlers returned by
:func:`~brizocast.bot.handlers.presets.build_preset_handlers` against a real
:class:`~brizocast.services.preset_service.PresetService` backed by a temp-file
SQLite database. Coverage:

* ``/presets`` lists default + custom presets and offers the pick keyboard
  (Req 4.1, 4.3);
* the custom-conditions conversation collects every field and persists the
  result (Req 4.5, 4.6) — verified by reading the row back;
* a maximum wave height below the minimum is rejected and re-requested inline
  (Req 4.8) without advancing the conversation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from telegram import InlineKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.bot.handlers.presets import (
    SUBSCRIPTION_ID_KEY,
    USER_ID_KEY,
    CustomConditionsState,
    build_preset_handlers,
)
from brizocast.database.bootstrap import bootstrap_database
from brizocast.database.session import (
    create_engine,
    create_session_factory,
    session_scope,
)
from brizocast.models.activity import Activity
from brizocast.models.location import Location
from brizocast.models.preset import Preset
from brizocast.models.subscription import Subscription
from brizocast.models.user import User
from brizocast.repositories.condition_repo import SqlAlchemyCustomConditionRepository
from brizocast.services.preset_service import PresetService

pytestmark = pytest.mark.integration


# -- minimal Telegram update/context fakes ------------------------------ #


class FakeMessage:
    """Captures ``reply_text`` calls and exposes an inbound ``text``."""

    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[tuple[str, InlineKeyboardMarkup | None]] = []

    async def reply_text(
        self, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ) -> None:
        self.replies.append((text, reply_markup))


class FakeUser:
    def __init__(self, user_id: int = 1, username: str = "tester") -> None:
        self.id = user_id
        self.username = username


class FakeUpdate:
    """Stand-in for :class:`telegram.Update` exposing the fields handlers read."""

    def __init__(self, message: FakeMessage, user: FakeUser | None = None) -> None:
        self._message = message
        self._user = user
        self.callback_query = None

    @property
    def effective_message(self) -> FakeMessage:
        return self._message

    @property
    def effective_user(self) -> FakeUser | None:
        return self._user


class FakeContext:
    """Stand-in for the bot context carrying the per-user ``user_data`` store."""

    def __init__(self, user_data: dict[Any, Any] | None = None) -> None:
        self.user_data: dict[Any, Any] = {} if user_data is None else user_data


def _as_update(update: FakeUpdate) -> Update:
    return cast(Update, update)


def _as_context(context: FakeContext) -> ContextTypes.DEFAULT_TYPE:
    return cast(ContextTypes.DEFAULT_TYPE, context)


# -- handler extraction helpers ----------------------------------------- #


def _presets_command(
    handlers: list[BaseHandler[Update, ContextTypes.DEFAULT_TYPE, Any]],
) -> CommandHandler[ContextTypes.DEFAULT_TYPE, Any]:
    for handler in handlers:
        if isinstance(handler, CommandHandler):
            return handler
    raise AssertionError("no /presets CommandHandler returned")


def _conversation(
    handlers: list[BaseHandler[Update, ContextTypes.DEFAULT_TYPE, Any]],
) -> ConversationHandler[ContextTypes.DEFAULT_TYPE]:
    for handler in handlers:
        if isinstance(handler, ConversationHandler):
            return handler
    raise AssertionError("no custom-conditions ConversationHandler returned")


def _state_callback(
    conversation: ConversationHandler[ContextTypes.DEFAULT_TYPE],
    state: CustomConditionsState,
) -> Any:
    return conversation.states[state][0].callback


# -- fixtures ----------------------------------------------------------- #


@pytest.fixture
async def session_factory(
    tmp_path: object,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = f"{tmp_path}/presets_handler.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await bootstrap_database(engine)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def _seed_subscription(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    telegram_user_id: int,
) -> tuple[int, int]:
    """Seed a user, activity, location, and subscription; return (user_id, sub_id)."""
    async with session_scope(session_factory) as session:
        user = User(telegram_user_id=telegram_user_id)
        session.add(user)
        activity = Activity(key="surf", display_name="🏄 Surf", available_in_mvp=True)
        session.add(activity)
        await session.flush()
        location = Location(user_id=user.id, lat=39.34, lon=-9.36)
        session.add(location)
        await session.flush()
        sub = Subscription(
            user_id=user.id,
            activity_id=activity.id,
            location_id=location.id,
        )
        session.add(sub)
        await session.flush()
        return user.id, sub.id


# -- /presets ----------------------------------------------------------- #


async def test_presets_command_lists_and_offers_keyboard(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """/presets renders the listing text and a pick keyboard (Req 4.1, 4.3)."""
    user_id, _ = await _seed_subscription(session_factory, telegram_user_id=801)
    async with session_scope(session_factory) as session:
        session.add(
            Preset(
                owner_user_id=user_id,
                name="My Custom",
                region=None,
                is_default=False,
                min_wave_m=1.0,
                max_wave_m=2.0,
                min_period_s=9.0,
                max_wind_kmh=20.0,
                preferred_wind_dir="E",
                preferred_swell_dir="W",
            )
        )

    handlers = build_preset_handlers(PresetService(session_factory))
    command = _presets_command(handlers)

    message = FakeMessage()
    context = FakeContext({USER_ID_KEY: user_id})
    await command.callback(_as_update(FakeUpdate(message)), _as_context(context))

    assert len(message.replies) == 1
    text, markup = message.replies[0]
    assert "Presets" in text
    assert "My Custom" in text
    assert isinstance(markup, InlineKeyboardMarkup)
    # At least the user's custom preset.
    assert sum(len(row) for row in markup.inline_keyboard) >= 1


async def test_presets_command_requires_known_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without a resolved user id, /presets asks the user to /start (handoff)."""
    handlers = build_preset_handlers(PresetService(session_factory))
    command = _presets_command(handlers)

    message = FakeMessage()
    await command.callback(
        _as_update(FakeUpdate(message)), _as_context(FakeContext())
    )

    assert len(message.replies) == 1
    assert "/start" in message.replies[0][0]


# -- custom-conditions conversation ------------------------------------- #


async def test_custom_conditions_happy_path_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Walking every step persists the conditions for the subscription (Req 4.5, 4.6)."""
    _, sub_id = await _seed_subscription(session_factory, telegram_user_id=802)
    conversation = _conversation(build_preset_handlers(PresetService(session_factory)))
    context = FakeContext({SUBSCRIPTION_ID_KEY: sub_id})

    async def feed(state: CustomConditionsState, text: str) -> int:
        callback = _state_callback(conversation, state)
        update = _as_update(FakeUpdate(FakeMessage(text)))
        return cast(int, await callback(update, _as_context(context)))

    # Entry point prompts for the first field.
    start = conversation.entry_points[0].callback
    next_state = cast(
        int,
        await start(_as_update(FakeUpdate(FakeMessage())), _as_context(context)),
    )
    assert next_state == CustomConditionsState.MIN_WAVE

    assert await feed(CustomConditionsState.MIN_WAVE, "1.0") == CustomConditionsState.MAX_WAVE
    assert await feed(CustomConditionsState.MAX_WAVE, "2.5") == CustomConditionsState.MIN_PERIOD
    assert await feed(CustomConditionsState.MIN_PERIOD, "10") == CustomConditionsState.MAX_WIND
    assert await feed(CustomConditionsState.MAX_WIND, "25") == CustomConditionsState.WIND_DIR
    assert await feed(CustomConditionsState.WIND_DIR, "E") == CustomConditionsState.SWELL_DIR
    assert await feed(CustomConditionsState.SWELL_DIR, "W") == CustomConditionsState.TIDE
    assert await feed(CustomConditionsState.TIDE, "mid") == CustomConditionsState.DAYLIGHT
    assert await feed(CustomConditionsState.DAYLIGHT, "yes") == ConversationHandler.END

    async with session_scope(session_factory) as session:
        repo = SqlAlchemyCustomConditionRepository(session)
        stored = await repo.get_for_subscription(sub_id)
    assert stored is not None
    assert stored.min_wave_m == pytest.approx(1.0)
    assert stored.max_wave_m == pytest.approx(2.5)
    assert stored.min_period_s == pytest.approx(10.0)
    assert stored.max_wind_kmh == pytest.approx(25.0)
    assert stored.acceptable_wind_dir == "E"
    assert stored.acceptable_swell_dir == "W"
    assert stored.tide_preference == "mid"
    assert stored.daylight_only is True
    # Draft and handoff key cleared on completion.
    assert SUBSCRIPTION_ID_KEY not in context.user_data


async def test_custom_conditions_rejects_inverted_wave_band(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A maximum below the minimum is re-requested inline, not advanced (Req 4.8)."""
    _, sub_id = await _seed_subscription(session_factory, telegram_user_id=803)
    conversation = _conversation(build_preset_handlers(PresetService(session_factory)))
    context = FakeContext({SUBSCRIPTION_ID_KEY: sub_id})

    min_cb = _state_callback(conversation, CustomConditionsState.MIN_WAVE)
    await min_cb(_as_update(FakeUpdate(FakeMessage("2.0"))), _as_context(context))

    max_cb = _state_callback(conversation, CustomConditionsState.MAX_WAVE)
    message = FakeMessage("1.0")  # below the 2.0 minimum
    next_state = cast(
        int, await max_cb(_as_update(FakeUpdate(message)), _as_context(context))
    )

    # Stays on the max-wave step and explains the problem.
    assert next_state == CustomConditionsState.MAX_WAVE
    assert message.replies
    assert "minimum" in message.replies[-1][0].lower()
    # Nothing persisted while the band is invalid.
    async with session_scope(session_factory) as session:
        repo = SqlAlchemyCustomConditionRepository(session)
        assert await repo.get_for_subscription(sub_id) is None


async def test_custom_conditions_requires_subscription(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Entering without a subscription handoff ends immediately (contract)."""
    conversation = _conversation(build_preset_handlers(PresetService(session_factory)))
    start = conversation.entry_points[0].callback

    message = FakeMessage()
    result = cast(
        int,
        await start(
            _as_update(FakeUpdate(message)), _as_context(FakeContext())
        ),
    )
    assert result == ConversationHandler.END
    assert message.replies
    assert "/add" in message.replies[0][0]
