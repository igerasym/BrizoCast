"""Unit tests for the custom-conditions input parsers (task 7.5).

Covers the pure parsing helpers backing the custom-conditions conversation
(Req 4.5): non-negative numbers, directions (compass / degrees / skip), tide
preference, and the daylight yes/no flag. These are exercised directly because
they are pure functions; the conversation flow itself is driven end-to-end in
the integration test.
"""

from __future__ import annotations

import pytest

from brizocast.activities.surf.conditions import TidePreference
from brizocast.bot.handlers.presets import (
    _parse_bool,
    _parse_direction,
    _parse_nonneg_float,
    _parse_tide,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0.0),
        ("1.5", 1.5),
        ("  2 ", 2.0),
    ],
)
def test_parse_nonneg_float_accepts_valid(text: str, expected: float) -> None:
    assert _parse_nonneg_float(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["-1", "abc", "", "nan", "inf"])
def test_parse_nonneg_float_rejects_invalid(text: str) -> None:
    assert _parse_nonneg_float(text) is None


def test_parse_direction_compass_point() -> None:
    ok, degrees = _parse_direction("NW")
    assert ok is True
    assert degrees == pytest.approx(315.0)


def test_parse_direction_degrees() -> None:
    ok, degrees = _parse_direction("180")
    assert ok is True
    assert degrees == pytest.approx(180.0)


@pytest.mark.parametrize("word", ["skip", "any", "none", "-", "SKIP"])
def test_parse_direction_skip_means_none(word: str) -> None:
    ok, degrees = _parse_direction(word)
    assert ok is True
    assert degrees is None


@pytest.mark.parametrize("text", ["361", "-5", "ZZ", "north-ish"])
def test_parse_direction_rejects_invalid(text: str) -> None:
    ok, degrees = _parse_direction(text)
    assert ok is False
    assert degrees is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("low", TidePreference.LOW),
        ("MID", TidePreference.MID),
        ("high", TidePreference.HIGH),
    ],
)
def test_parse_tide_valid(text: str, expected: TidePreference) -> None:
    ok, tide = _parse_tide(text)
    assert ok is True
    assert tide is expected


def test_parse_tide_skip_means_none() -> None:
    ok, tide = _parse_tide("skip")
    assert ok is True
    assert tide is None


def test_parse_tide_rejects_unknown() -> None:
    ok, tide = _parse_tide("rising")
    assert ok is False
    assert tide is None


@pytest.mark.parametrize("text", ["yes", "y", "true", "1", "on", "YES"])
def test_parse_bool_true(text: str) -> None:
    assert _parse_bool(text) is True


@pytest.mark.parametrize("text", ["no", "n", "false", "0", "off"])
def test_parse_bool_false(text: str) -> None:
    assert _parse_bool(text) is False


@pytest.mark.parametrize("text", ["maybe", "", "yep"])
def test_parse_bool_unrecognised(text: str) -> None:
    assert _parse_bool(text) is None
