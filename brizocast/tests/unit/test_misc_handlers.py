"""Unit tests for the misc handlers (task 7.9).

Covers the thin adapters built by
:mod:`brizocast.bot.handlers.help` and :mod:`brizocast.bot.handlers.feedback`:

* ``/help`` lists every supported command with a description (Req 13.1, 13.2).
* the unknown-command fallback tells the user and suggests ``/help`` (Req 13.7).
* the 👍/👎 feedback callback parses its callback data and persists the rating
  through :class:`FeedbackService` (Req 12.3, 12.4).
* :func:`build_misc_handlers` aggregates the above with the fallback last.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from telegram import Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
)

from brizocast.bot.formatters.commands import COMMANDS
from brizocast.bot.handlers.feedback import build_feedback_handlers
from brizocast.bot.handlers.help import (
    build_help_handlers,
    build_misc_handlers,
    build_unknown_command_handler,
)
from brizocast.bot.keyboards.feedback import (
    FeedbackCallbackData,
    encode_feedback_callback,
)
from brizocast.models.feedback import Feedback, FeedbackRating
from brizocast.services.feedback_service import FeedbackService

pytestmark = pytest.mark.unit

_SUB_ID = 42
_SPOT_KEY = "pt-lisbon-carcavelos"
_SCORE = 88


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class FakeFeedbackService:
    """Records ``record_feedback`` calls; stands in for :class:`FeedbackService`."""

    calls: list[tuple[int, str, int, FeedbackRating | str]] = field(default_factory=list)

    async def record_feedback(
        self,
        subscription_id: int,
        spot_key: str,
        surf_score: int,
        rating: FeedbackRating | str,
    ) -> Feedback:
        self.calls.append((subscription_id, spot_key, surf_score, rating))
        return Feedback(
            subscription_id=subscription_id,
            spot_key=spot_key,
            surf_score=surf_score,
            rating=rating if isinstance(rating, FeedbackRating) else FeedbackRating(rating),
        )


@dataclass
class FakeMessage:
    """Captures ``reply_text`` calls."""

    text: str | None = None
    replies: list[str] = field(default_factory=list)

    async def reply_text(self, text: str, reply_markup: object | None = None) -> None:
        self.replies.append(text)


@dataclass
class FakeQuery:
    """Captures ``answer`` for a callback tap."""

    data: str | None
    answers: list[str | None] = field(default_factory=list)

    async def answer(self, text: str | None = None) -> None:
        self.answers.append(text)


@dataclass
class FakeUpdate:
    """Minimal stand-in carrying just what the handlers read."""

    effective_message: FakeMessage | None = None
    callback_query: FakeQuery | None = None


@dataclass
class FakeContext:
    """Minimal stand-in exposing ``user_data``."""

    user_data: dict[Any, Any] = field(default_factory=dict)


def _callback(handler: BaseHandler[Update, ContextTypes.DEFAULT_TYPE, object]) -> Any:
    return handler.callback


async def _run(cb: Any, update: FakeUpdate, context: FakeContext) -> Any:
    return await cb(cast(Update, update), cast("ContextTypes.DEFAULT_TYPE", context))


# --------------------------------------------------------------------------- #
# /help (Req 13.1, 13.2)
# --------------------------------------------------------------------------- #


async def test_help_lists_every_command_with_description() -> None:
    """``/help`` lists each supported command and a description (Req 13.1, 13.2)."""
    handlers = build_help_handlers()
    assert len(handlers) == 1
    assert isinstance(handlers[0], CommandHandler)

    message = FakeMessage()
    await _run(_callback(handlers[0]), FakeUpdate(effective_message=message), FakeContext())

    assert len(message.replies) == 1
    reply = message.replies[0]
    # Every command in the source-of-truth set and its description appears.
    for command, description in COMMANDS:
        assert command in reply
        assert description in reply


def test_help_command_set_is_complete() -> None:
    """The command set covers the full list required by Req 13.1."""
    commands = {command for command, _ in COMMANDS}
    assert commands == {
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


# --------------------------------------------------------------------------- #
# Unknown-command fallback (Req 13.7)
# --------------------------------------------------------------------------- #


async def test_unknown_command_suggests_help() -> None:
    """An unrecognised command is reported and ``/help`` is suggested (Req 13.7)."""
    handler = build_unknown_command_handler()
    assert isinstance(handler, MessageHandler)

    message = FakeMessage(text="/wat")
    await _run(_callback(handler), FakeUpdate(effective_message=message), FakeContext())

    assert len(message.replies) == 1
    assert "/help" in message.replies[0]


# --------------------------------------------------------------------------- #
# Feedback callback (Req 12.3, 12.4)
# --------------------------------------------------------------------------- #


def _feedback_data(rating: FeedbackRating) -> str:
    return encode_feedback_callback(
        FeedbackCallbackData(
            subscription_id=_SUB_ID,
            spot_key=_SPOT_KEY,
            surf_score=_SCORE,
            rating=rating,
        )
    )


async def test_feedback_callback_parses_and_records_up() -> None:
    """A thumbs-up tap parses the payload and persists the rating (Req 12.4)."""
    service = FakeFeedbackService()
    handlers = build_feedback_handlers(cast(FeedbackService, service))
    assert len(handlers) == 1
    assert isinstance(handlers[0], CallbackQueryHandler)

    query = FakeQuery(data=_feedback_data(FeedbackRating.UP))
    await _run(_callback(handlers[0]), FakeUpdate(callback_query=query), FakeContext())

    assert service.calls == [(_SUB_ID, _SPOT_KEY, _SCORE, FeedbackRating.UP)]
    # The tap is acknowledged with a brief thanks toast (Req 12.3).
    assert query.answers and query.answers[0] is not None


async def test_feedback_callback_records_down() -> None:
    """A thumbs-down tap persists the DOWN rating (Req 12.4)."""
    service = FakeFeedbackService()
    handlers = build_feedback_handlers(cast(FeedbackService, service))

    query = FakeQuery(data=_feedback_data(FeedbackRating.DOWN))
    await _run(_callback(handlers[0]), FakeUpdate(callback_query=query), FakeContext())

    assert service.calls == [(_SUB_ID, _SPOT_KEY, _SCORE, FeedbackRating.DOWN)]


async def test_feedback_callback_ignores_malformed_payload() -> None:
    """A malformed feedback payload is acknowledged but not persisted."""
    service = FakeFeedbackService()
    handlers = build_feedback_handlers(cast(FeedbackService, service))

    query = FakeQuery(data="fb:1:u:not-an-int:88:spot")
    await _run(_callback(handlers[0]), FakeUpdate(callback_query=query), FakeContext())

    assert service.calls == []
    assert query.answers  # spinner stopped


# --------------------------------------------------------------------------- #
# Aggregator ordering
# --------------------------------------------------------------------------- #


def test_misc_handlers_put_fallback_last() -> None:
    """``build_misc_handlers`` returns the catch-all fallback last."""
    service = FakeFeedbackService()
    handlers = build_misc_handlers(cast(FeedbackService, service))

    # /help command, feedback callback, then the unknown-command MessageHandler.
    assert isinstance(handlers[0], CommandHandler)
    assert isinstance(handlers[-1], MessageHandler)
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)
