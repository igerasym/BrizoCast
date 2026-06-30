"""Unit tests for the explainable alert formatter and feedback keyboard (task 5.9).

Covers Requirements 12.1, 12.2, 12.3:

* alert text includes the surf score, score category, surf-spot name, and
  forecast window (12.1), plus the wave/period/wind per-factor breakdown (12.2);
* the inline keyboard offers 👍 and 👎 controls whose callback data round-trips
  back to the originating subscription, spot, and score (12.3).

The numbered correctness property (Property 27) is covered by the Hypothesis
test in task 5.10; these are illustrative example/edge-case checks.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brizocast.bot.formatters.alerts import build_alert_message, format_alert_text
from brizocast.bot.keyboards.feedback import (
    CALLBACK_PREFIX,
    TELEGRAM_CALLBACK_DATA_MAX_BYTES,
    FeedbackCallbackData,
    build_feedback_keyboard,
    encode_feedback_callback,
    is_feedback_callback,
    parse_feedback_callback,
)
from brizocast.core.domain.forecast import ForecastStep, ForecastWindow
from brizocast.core.domain.scoring import (
    ScoreBreakdown,
    ScoreCategory,
    ScoreResult,
)
from brizocast.core.domain.scoring_types import FactorContribution
from brizocast.core.domain.spot import SurfSpot
from brizocast.models.feedback import FeedbackRating

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #
TS = datetime(2025, 6, 21, 6, 0, tzinfo=UTC)


def _spot(**overrides: object) -> SurfSpot:
    base: dict[str, object] = {
        "spot_key": "ericeira-ribeira-dilhas",
        "name": "Ribeira d'Ilhas",
        "lat": 38.98,
        "lon": -9.42,
    }
    base.update(overrides)
    return SurfSpot(**base)  # type: ignore[arg-type]


def _step(**overrides: object) -> ForecastStep:
    base: dict[str, object] = {
        "timestamp": TS,
        "wave_height_m": 1.6,
        "swell_period_s": 12.0,
        "swell_direction_deg": 270.0,
        "wind_speed_kmh": 15.0,
        "wind_direction_deg": 315.0,  # NW
    }
    base.update(overrides)
    return ForecastStep(**base)  # type: ignore[arg-type]


def _result(score: int = 87, **window_overrides: object) -> ScoreResult:
    breakdown = ScoreBreakdown(
        wave_height=FactorContribution(value=1.0, weight=0.30),
        swell_period=FactorContribution(value=0.9, weight=0.25),
        wind_speed=FactorContribution(value=0.5, weight=0.20),
        wind_direction=FactorContribution(value=0.8, weight=0.15),
        swell_direction=FactorContribution(value=1.0, weight=0.10),
        total_weighted=0.87,
    )
    window_kwargs: dict[str, object] = {"start": TS, "end": TS}
    window_kwargs.update(window_overrides)
    return ScoreResult(
        score=score,
        category=ScoreCategory.from_score(score),
        breakdown=breakdown,
        forecast_window=ForecastWindow(**window_kwargs),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Alert text content (Req 12.1, 12.2)
# --------------------------------------------------------------------------- #
def test_alert_text_includes_score_category_spot_and_window() -> None:
    text = format_alert_text(_spot(), _result(score=87), _step())
    assert "Ribeira d'Ilhas" in text  # spot name (12.1)
    assert "Score 87" in text  # surf score (12.1)
    assert "Excellent" in text  # score category (12.1)
    assert "2025-06-21 06:00 UTC" in text  # forecast window (12.1)


def test_alert_text_includes_wave_period_and_wind_breakdown() -> None:
    text = format_alert_text(_spot(), _result(), _step())
    # Raw human-readable values from the forecast step.
    assert "1.6m" in text
    assert "12s" in text
    assert "15km/h" in text
    # Per-factor breakdown contributions produced by the scoring engine (12.2).
    assert "wave 100%" in text
    assert "period 90%" in text
    assert "wind 50%" in text


def test_alert_text_renders_wind_compass_direction() -> None:
    text = format_alert_text(_spot(), _result(), _step(wind_direction_deg=315.0))
    assert "NW" in text


def test_alert_text_category_matches_score_band() -> None:
    assert "Perfect" in format_alert_text(_spot(), _result(score=97), _step())
    assert "Good" in format_alert_text(_spot(), _result(score=72), _step())
    assert "Rideable" in format_alert_text(_spot(), _result(score=55), _step())


def test_alert_text_renders_interval_window_same_day() -> None:
    end = datetime(2025, 6, 21, 9, 0, tzinfo=UTC)
    text = format_alert_text(_spot(), _result(end=end), _step())
    assert "2025-06-21 06:00\u201309:00 UTC" in text


# --------------------------------------------------------------------------- #
# Feedback keyboard (Req 12.3)
# --------------------------------------------------------------------------- #
def test_keyboard_has_thumbs_up_and_down_buttons() -> None:
    markup = build_feedback_keyboard(subscription_id=42, spot_key="spot-1", surf_score=87)
    buttons = [button for row in markup.inline_keyboard for button in row]
    labels = {button.text for button in buttons}
    assert labels == {"👍", "👎"}


def test_keyboard_callback_data_round_trips_identity() -> None:
    markup = build_feedback_keyboard(subscription_id=42, spot_key="spot-1", surf_score=87)
    parsed = [
        parse_feedback_callback(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if isinstance(button.callback_data, str)
    ]
    ratings = {data.rating for data in parsed}
    assert ratings == {FeedbackRating.UP, FeedbackRating.DOWN}
    for data in parsed:
        assert data.subscription_id == 42
        assert data.spot_key == "spot-1"
        assert data.surf_score == 87


def test_callback_encode_parse_round_trip() -> None:
    original = FeedbackCallbackData(
        subscription_id=7,
        spot_key="ericeira-ribeira-dilhas",
        surf_score=91,
        rating=FeedbackRating.DOWN,
    )
    assert parse_feedback_callback(encode_feedback_callback(original)) == original


def test_callback_preserves_spot_key_containing_separator() -> None:
    original = FeedbackCallbackData(
        subscription_id=3,
        spot_key="region:spot:weird",
        surf_score=70,
        rating=FeedbackRating.UP,
    )
    parsed = parse_feedback_callback(encode_feedback_callback(original))
    assert parsed.spot_key == "region:spot:weird"


def test_is_feedback_callback_discriminates_scheme() -> None:
    raw = encode_feedback_callback(
        FeedbackCallbackData(subscription_id=1, spot_key="s", surf_score=60, rating=FeedbackRating.UP)
    )
    assert is_feedback_callback(raw) is True
    assert is_feedback_callback("other:1:foo") is False
    assert is_feedback_callback("nav:next") is False


def test_encode_rejects_oversized_payload() -> None:
    with pytest.raises(ValueError, match="64-byte limit"):
        encode_feedback_callback(
            FeedbackCallbackData(
                subscription_id=1,
                spot_key="x" * 80,
                surf_score=99,
                rating=FeedbackRating.UP,
            )
        )


def test_encoded_callback_within_telegram_limit() -> None:
    raw = encode_feedback_callback(
        FeedbackCallbackData(
            subscription_id=999999,
            spot_key="ericeira-ribeira-dilhas",
            surf_score=100,
            rating=FeedbackRating.DOWN,
        )
    )
    assert len(raw.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_MAX_BYTES
    assert raw.startswith(f"{CALLBACK_PREFIX}:")


@pytest.mark.parametrize(
    "raw",
    [
        "fb:1:u:42:87",  # too few fields
        "fb:2:u:42:87:spot",  # unsupported version
        "xx:1:u:42:87:spot",  # wrong prefix
        "fb:1:x:42:87:spot",  # unknown rating token
        "fb:1:u:notint:87:spot",  # non-integer subscription id
        "fb:1:u:42:87:",  # empty spot key
    ],
)
def test_parse_rejects_malformed_callback(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_feedback_callback(raw)


# --------------------------------------------------------------------------- #
# Combined alert message (text + keyboard)
# --------------------------------------------------------------------------- #
def test_build_alert_message_pairs_text_and_keyboard() -> None:
    text, markup = build_alert_message(_spot(), _result(score=87), _step(), subscription_id=42)
    assert "Ribeira d'Ilhas" in text
    assert "Score 87" in text
    labels = {button.text for row in markup.inline_keyboard for button in row}
    assert labels == {"👍", "👎"}
    # Keyboard identity matches the alert it accompanies.
    parsed = [
        parse_feedback_callback(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if isinstance(button.callback_data, str)
    ]
    assert all(data.spot_key == "ericeira-ribeira-dilhas" for data in parsed)
    assert all(data.surf_score == 87 for data in parsed)
    assert all(data.subscription_id == 42 for data in parsed)
