"""Unit tests for conversational inline-keyboard builders and codecs (task 7.1).

Covers the keyboard builders in :mod:`brizocast.bot.keyboards` and their
callback-data codecs (Req 13.6): every keyboard renders the expected buttons,
and every button's callback data round-trips back to a parseable selection that
is namespaced distinctly from the feedback ``"fb"`` scheme.
"""

from __future__ import annotations

from typing import Any

import pytest

from brizocast.activities.base import Activity
from brizocast.core.domain.conditions import ConditionsModel
from brizocast.core.ports.scorer import Scorer
from brizocast.bot.keyboards.activities import (
    ActivitySelection,
    build_activity_keyboard,
    encode_activity_callback,
    parse_activity_callback,
)
from brizocast.bot.keyboards.callbacks import (
    CALLBACK_VERSION,
    TELEGRAM_CALLBACK_DATA_MAX_BYTES,
    callback_namespace,
)
from brizocast.bot.keyboards.common import (
    ConfirmCallbackData,
    build_confirm_keyboard,
    build_single_choice_keyboard,
    parse_confirm_callback,
)
from brizocast.bot.keyboards.feedback import is_feedback_callback
from brizocast.bot.keyboards.locations import (
    LocationOption,
    build_location_options_keyboard,
    parse_location_callback,
)
from brizocast.bot.keyboards.notifications import (
    build_notification_mode_keyboard,
    parse_notification_mode_callback,
)
from brizocast.bot.keyboards.presets import (
    PresetPick,
    build_preset_pick_keyboard,
    parse_preset_callback,
)
from brizocast.bot.keyboards.subscriptions import (
    SubscriptionPickPurpose,
    build_subscription_pick_keyboard,
    parse_subscription_callback,
)
from brizocast.notifications.modes import NotificationMode
from brizocast.services.preset_service import PresetOption, PresetSource
from brizocast.services.subscription_service import SubscriptionSummary

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakeActivity(Activity[Any]):
    """Minimal concrete activity for keyboard rendering; methods unused here."""

    def scorer(self) -> Scorer[Any]:  # pragma: no cover - not exercised here
        raise NotImplementedError

    def conditions_schema(self) -> type[ConditionsModel]:  # pragma: no cover
        raise NotImplementedError

    def default_forecast_provider_key(self) -> str:  # pragma: no cover
        raise NotImplementedError


def _activity(key: str, display_name: str, available: bool) -> Activity[Any]:
    """Build a concrete :class:`Activity` with the given class attributes."""

    subclass = type(
        f"_Activity_{key}",
        (_FakeActivity,),
        {"key": key, "display_name": display_name, "available_in_mvp": available},
    )
    instance: Activity[Any] = subclass()
    return instance


def _summary(subscription_id: int = 1, **overrides: object) -> SubscriptionSummary:
    base: dict[str, object] = {
        "subscription_id": subscription_id,
        "activity_key": "surf",
        "activity_display_name": "🏄 Surf",
        "location_label": "Ericeira",
        "location_place": "Ericeira, Portugal",
        "search_radius_km": 30.0,
        "notification_mode": NotificationMode.IMMEDIATE.value,
    }
    base.update(overrides)
    return SubscriptionSummary(**base)  # type: ignore[arg-type]


def _preset(name: str, *, preset_id: int | None, **overrides: object) -> PresetOption:
    base: dict[str, object] = {
        "name": name,
        "region": "Portugal",
        "params": object(),
        "source": PresetSource.STATIC_DEFAULT,
        "preset_id": preset_id,
        "ai_generated": False,
    }
    base.update(overrides)
    return PresetOption(**base)  # type: ignore[arg-type]


def _buttons(markup: object) -> list[object]:
    return [button for row in markup.inline_keyboard for button in row]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Generic single-choice keyboard
# --------------------------------------------------------------------------- #
def test_single_choice_lays_out_choices() -> None:
    markup = build_single_choice_keyboard([("A", "x:1:a"), ("B", "x:1:b")])
    buttons = _buttons(markup)
    assert [b.text for b in buttons] == ["A", "B"]  # type: ignore[attr-defined]
    assert [b.callback_data for b in buttons] == ["x:1:a", "x:1:b"]  # type: ignore[attr-defined]


def test_single_choice_respects_columns() -> None:
    markup = build_single_choice_keyboard(
        [("A", "a"), ("B", "b"), ("C", "c")], columns=2
    )
    rows = markup.inline_keyboard
    assert len(rows[0]) == 2
    assert len(rows[1]) == 1


def test_single_choice_rejects_empty_and_bad_columns() -> None:
    with pytest.raises(ValueError):
        build_single_choice_keyboard([])
    with pytest.raises(ValueError):
        build_single_choice_keyboard([("A", "a")], columns=0)


def test_single_choice_rejects_oversized_callback() -> None:
    with pytest.raises(ValueError, match="64-byte limit"):
        build_single_choice_keyboard([("A", "x" * 80)])


# --------------------------------------------------------------------------- #
# Activity selection (Req 1.1, 1.3, 1.4)
# --------------------------------------------------------------------------- #
def test_activity_keyboard_marks_unavailable_and_round_trips() -> None:
    activities = [
        _activity("surf", "🏄 Surf", available=True),
        _activity("snowboard", "🏂 Snowboard", available=False),
    ]
    markup = build_activity_keyboard(activities)
    buttons = _buttons(markup)

    surf, snow = buttons
    assert "🔒" not in surf.text  # type: ignore[attr-defined]
    assert "🔒" in snow.text  # type: ignore[attr-defined]

    surf_sel = parse_activity_callback(surf.callback_data)  # type: ignore[attr-defined]
    snow_sel = parse_activity_callback(snow.callback_data)  # type: ignore[attr-defined]
    assert surf_sel == ActivitySelection(activity_key="surf", available=True)
    assert snow_sel == ActivitySelection(activity_key="snowboard", available=False)


def test_activity_callback_round_trip() -> None:
    raw = encode_activity_callback("surf", available=True)
    assert parse_activity_callback(raw) == ActivitySelection("surf", True)


# --------------------------------------------------------------------------- #
# Location options (Req 2.1)
# --------------------------------------------------------------------------- #
def test_location_keyboard_offers_all_options() -> None:
    markup = build_location_options_keyboard()
    parsed = {
        parse_location_callback(b.callback_data) for b in _buttons(markup)  # type: ignore[attr-defined]
    }
    assert parsed == set(LocationOption)


# --------------------------------------------------------------------------- #
# Subscription pick (Req 3.6, 13.4, 13.5)
# --------------------------------------------------------------------------- #
def test_subscription_pick_round_trips_id_and_purpose() -> None:
    summaries = [_summary(1), _summary(2, location_label="Peniche")]
    markup = build_subscription_pick_keyboard(summaries, SubscriptionPickPurpose.REMOVE)
    picks = [
        parse_subscription_callback(b.callback_data) for b in _buttons(markup)  # type: ignore[attr-defined]
    ]
    assert [p.subscription_id for p in picks] == [1, 2]
    assert all(p.purpose is SubscriptionPickPurpose.REMOVE for p in picks)


def test_subscription_pick_rejects_empty() -> None:
    with pytest.raises(ValueError):
        build_subscription_pick_keyboard([], SubscriptionPickPurpose.FORECAST)


# --------------------------------------------------------------------------- #
# Preset pick (Req 4.3)
# --------------------------------------------------------------------------- #
def test_preset_pick_round_trips_index_and_id() -> None:
    options = [
        _preset("Beach break", preset_id=None),  # static default, no id
        _preset("AI Reef", preset_id=7, source=PresetSource.PERSISTED_DEFAULT, ai_generated=True),
    ]
    markup = build_preset_pick_keyboard(options)
    buttons = _buttons(markup)
    picks = [parse_preset_callback(b.callback_data) for b in buttons]  # type: ignore[attr-defined]
    assert picks == [PresetPick(index=0, preset_id=None), PresetPick(index=1, preset_id=7)]
    assert "✨" in buttons[1].text  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Notification mode (Req 10.1, 10.2)
# --------------------------------------------------------------------------- #
def test_notification_mode_keyboard_round_trips_every_mode() -> None:
    markup = build_notification_mode_keyboard()
    parsed = [
        parse_notification_mode_callback(b.callback_data) for b in _buttons(markup)  # type: ignore[attr-defined]
    ]
    assert parsed == list(NotificationMode)


# --------------------------------------------------------------------------- #
# Confirm (Req 3.6, 13.6)
# --------------------------------------------------------------------------- #
def test_confirm_keyboard_round_trips_answer_and_action() -> None:
    markup = build_confirm_keyboard("remove:42")
    parsed = [
        parse_confirm_callback(b.callback_data) for b in _buttons(markup)  # type: ignore[attr-defined]
    ]
    assert ConfirmCallbackData(answer=True, action="remove:42") in parsed
    assert ConfirmCallbackData(answer=False, action="remove:42") in parsed


def test_confirm_action_may_contain_separator() -> None:
    raw = build_confirm_keyboard("a:b:c").inline_keyboard[0][0].callback_data
    assert isinstance(raw, str)
    assert parse_confirm_callback(raw).action == "a:b:c"


# --------------------------------------------------------------------------- #
# Namespacing — every scheme is distinct from feedback's "fb"
# --------------------------------------------------------------------------- #
def test_all_namespaces_distinct_from_feedback() -> None:
    raws = [
        encode_activity_callback("surf", available=True),
        build_location_options_keyboard().inline_keyboard[0][0].callback_data,
        build_subscription_pick_keyboard([_summary()], SubscriptionPickPurpose.SETTINGS)
        .inline_keyboard[0][0]
        .callback_data,
        build_preset_pick_keyboard([_preset("p", preset_id=1)]).inline_keyboard[0][0].callback_data,
        build_notification_mode_keyboard().inline_keyboard[0][0].callback_data,
        build_confirm_keyboard("act").inline_keyboard[0][0].callback_data,
    ]
    namespaces = set()
    for raw in raws:
        assert isinstance(raw, str)
        assert is_feedback_callback(raw) is False  # never collides with feedback
        ns = callback_namespace(raw)
        assert ns is not None
        assert ns != "fb"
        assert len(raw.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_MAX_BYTES
        namespaces.add(ns)
    # Each builder uses its own distinct namespace.
    assert len(namespaces) == len(raws)
    # All payloads carry the current scheme version.
    assert all(raw.split(":")[1] == CALLBACK_VERSION for raw in raws if isinstance(raw, str))
