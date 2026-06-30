"""Unit tests for the ``/status`` and ``/forecast`` handlers (task 7.7).

Drive the handler callbacks pulled off the assembled handlers against fakes for
the :class:`StatusService`, :class:`SubscriptionService`, and the telegram
``Update``/``Context`` objects, verifying:

* ``/status`` reports the active-subscription count and last scheduler-run time
  via :func:`format_status` (Req 13.3), and asks the user to ``/start`` first
  when the user id is missing;
* ``/forecast`` shows a subscription pick, then reports the best score/spot via
  :func:`format_forecast_result` (or the no-spots notice) regardless of
  mute/snooze, and handles a vanished subscription (Req 13.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from telegram import InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from brizocast.bot.formatters.commands import (
    format_forecast_no_spots,
    format_forecast_result,
    format_status,
)
from brizocast.bot.handlers.status import build_status_handlers
from brizocast.bot.keyboards.subscriptions import (
    SubscriptionPickPurpose,
    encode_subscription_callback,
)
from brizocast.core.domain.scoring import ScoreCategory
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.errors import NotFoundError
from brizocast.services.status_service import BestForecast
from brizocast.services.subscription_service import SubscriptionSummary

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeStatusService:
    def __init__(
        self,
        *,
        count: int = 0,
        last_run: datetime | None = None,
        best: BestForecast | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._count = count
        self._last_run = last_run
        self._best = best
        self._raises = raises
        self.best_calls: list[int] = []

    async def active_subscription_count(self, user_id: int) -> int:
        return self._count

    async def last_scheduler_run(self) -> datetime | None:
        return self._last_run

    async def best_forecast_for_subscription(
        self, subscription_id: int
    ) -> BestForecast:
        self.best_calls.append(subscription_id)
        if self._raises is not None:
            raise self._raises
        assert self._best is not None
        return self._best


class _FakeSubscriptionService:
    def __init__(self, summaries: list[SubscriptionSummary]) -> None:
        self._summaries = summaries

    async def summarize_for_user(self, user_id: int) -> list[SubscriptionSummary]:
        return self._summaries


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
class _FakeCallbackQuery:
    data: str
    edits: list[_SentMessage] = field(default_factory=list)
    answered: int = 0

    async def answer(self) -> None:
        self.answered += 1

    async def edit_message_text(self, text: str, reply_markup: Any = None) -> None:
        self.edits.append(_SentMessage(text=text, reply_markup=reply_markup))


@dataclass
class _FakeUpdate:
    effective_message: _FakeMessage | None = None
    callback_query: _FakeCallbackQuery | None = None


class _FakeContext:
    def __init__(self, user_data: dict[Any, Any] | None = None) -> None:
        self.user_data: dict[Any, Any] = {} if user_data is None else user_data


# --------------------------------------------------------------------------- #
# Helpers to pull callbacks off the assembled handlers
# --------------------------------------------------------------------------- #
def _status_callback(status: Any, subs: Any) -> Any:
    handlers = build_status_handlers(status, subs)
    return handlers[0].callback


def _forecast_entry(status: Any, subs: Any) -> Any:
    handlers = build_status_handlers(status, subs)
    conv = cast("ConversationHandler[Any]", handlers[1])
    return conv.entry_points[0].callback


def _forecast_pick(status: Any, subs: Any) -> Any:
    handlers = build_status_handlers(status, subs)
    conv = cast("ConversationHandler[Any]", handlers[1])
    return conv.states[0][0].callback


def _summary(sub_id: int) -> SubscriptionSummary:
    return SubscriptionSummary(
        subscription_id=sub_id,
        activity_key="surf",
        activity_display_name="🏄 Surf",
        location_label="Peniche",
        location_place="Peniche, Portugal",
        search_radius_km=30.0,
        notification_mode="immediate",
    )


# --------------------------------------------------------------------------- #
# /status (Req 13.3)
# --------------------------------------------------------------------------- #
async def test_status_reports_count_and_last_run() -> None:
    when = datetime(2025, 6, 21, 6, 30, tzinfo=UTC)
    status = _FakeStatusService(count=3, last_run=when)
    subs = _FakeSubscriptionService([])
    message = _FakeMessage()
    update = _FakeUpdate(effective_message=message)
    context = _FakeContext({"db_user_id": 11})

    await _status_callback(status, subs)(update, context)

    assert len(message.sent) == 1
    assert message.sent[0].text == format_status(3, when)


async def test_status_without_user_asks_to_start() -> None:
    status = _FakeStatusService(count=0, last_run=None)
    subs = _FakeSubscriptionService([])
    message = _FakeMessage()
    update = _FakeUpdate(effective_message=message)

    await _status_callback(status, subs)(update, _FakeContext())

    assert len(message.sent) == 1
    assert "/start" in message.sent[0].text


# --------------------------------------------------------------------------- #
# /forecast entry (Req 13.4)
# --------------------------------------------------------------------------- #
async def test_forecast_entry_shows_subscription_pick() -> None:
    status = _FakeStatusService()
    subs = _FakeSubscriptionService([_summary(5), _summary(6)])
    message = _FakeMessage()
    update = _FakeUpdate(effective_message=message)
    context = _FakeContext({"db_user_id": 11})

    state = await _forecast_entry(status, subs)(update, context)

    assert state == 0  # _PICK_SUBSCRIPTION
    assert len(message.sent) == 1
    assert isinstance(message.sent[0].reply_markup, InlineKeyboardMarkup)


async def test_forecast_entry_with_no_subscriptions_ends() -> None:
    status = _FakeStatusService()
    subs = _FakeSubscriptionService([])
    message = _FakeMessage()
    update = _FakeUpdate(effective_message=message)
    context = _FakeContext({"db_user_id": 11})

    state = await _forecast_entry(status, subs)(update, context)

    assert state == ConversationHandler.END
    assert len(message.sent) == 1
    assert "/add" in message.sent[0].text


async def test_forecast_entry_without_user_asks_to_start() -> None:
    status = _FakeStatusService()
    subs = _FakeSubscriptionService([_summary(5)])
    message = _FakeMessage()
    update = _FakeUpdate(effective_message=message)

    state = await _forecast_entry(status, subs)(update, _FakeContext())

    assert state == ConversationHandler.END
    assert "/start" in message.sent[0].text


# --------------------------------------------------------------------------- #
# /forecast pick (Req 13.4)
# --------------------------------------------------------------------------- #
async def test_forecast_pick_reports_best_score_and_spot() -> None:
    best = BestForecast(
        subscription_id=5,
        location_label="Peniche",
        has_nearby_spots=True,
        spot=SurfSpot(spot_key="pt/b", name="Supertubos", lat=39.34, lon=-9.35),
        score=88,
        category=ScoreCategory.EXCELLENT,
    )
    status = _FakeStatusService(best=best)
    subs = _FakeSubscriptionService([_summary(5)])
    query = _FakeCallbackQuery(
        data=encode_subscription_callback(SubscriptionPickPurpose.FORECAST, 5)
    )
    update = _FakeUpdate(callback_query=query)

    state = await _forecast_pick(status, subs)(update, _FakeContext())

    assert state == ConversationHandler.END
    assert status.best_calls == [5]  # bypasses mute/snooze; queries directly
    assert query.answered == 1
    assert query.edits[0].text == format_forecast_result(
        location_label="Peniche",
        spot_name="Supertubos",
        score=88,
        category_label="Excellent",
    )


async def test_forecast_pick_reports_no_spots() -> None:
    best = BestForecast(
        subscription_id=5,
        location_label="Peniche",
        has_nearby_spots=False,
        spot=None,
        score=None,
        category=None,
    )
    status = _FakeStatusService(best=best)
    subs = _FakeSubscriptionService([_summary(5)])
    query = _FakeCallbackQuery(
        data=encode_subscription_callback(SubscriptionPickPurpose.FORECAST, 5)
    )
    update = _FakeUpdate(callback_query=query)

    await _forecast_pick(status, subs)(update, _FakeContext())

    assert query.edits[0].text == format_forecast_no_spots("Peniche")


async def test_forecast_pick_handles_missing_subscription() -> None:
    status = _FakeStatusService(raises=NotFoundError("gone"))
    subs = _FakeSubscriptionService([_summary(5)])
    query = _FakeCallbackQuery(
        data=encode_subscription_callback(SubscriptionPickPurpose.FORECAST, 5)
    )
    update = _FakeUpdate(callback_query=query)

    state = await _forecast_pick(status, subs)(update, _FakeContext())

    assert state == ConversationHandler.END
    assert "no longer exists" in query.edits[0].text.lower()
