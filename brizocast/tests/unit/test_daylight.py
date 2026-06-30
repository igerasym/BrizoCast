"""Unit tests for the sunrise/sunset computation (task 2.4).

Exercises :func:`compute_daylight` for mid-latitude days (sunrise precedes
sunset with a plausible day length), the polar-night convention (empty interval,
``is_daylight`` always ``False``), and the midnight-sun convention (full-day
interval, ``is_daylight`` always ``True``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from brizocast.core.domain import GeoPoint, compute_daylight

pytestmark = pytest.mark.unit


def _day_length_hours(point: GeoPoint, on_date: date) -> float:
    info = compute_daylight(point, on_date)
    return (info.sunset - info.sunrise).total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# Mid-latitude sanity checks
# --------------------------------------------------------------------------- #
def test_midlatitude_summer_day_has_sunrise_before_sunset() -> None:
    # Hossegor, France (~43.7N) on the June solstice.
    point = GeoPoint(lat=43.66, lon=-1.43)
    info = compute_daylight(point, date(2025, 6, 21))
    assert info.sunrise < info.sunset
    # Both instants are timezone-aware UTC.
    assert info.sunrise.tzinfo is not None
    assert info.sunset.tzinfo is not None


def test_midlatitude_summer_day_length_is_reasonable() -> None:
    # Northern-hemisphere summer solstice -> long day (roughly 14-16 h at 43N).
    point = GeoPoint(lat=43.66, lon=-1.43)
    hours = _day_length_hours(point, date(2025, 6, 21))
    assert 14.0 <= hours <= 16.0


def test_midlatitude_winter_day_is_shorter_than_summer() -> None:
    point = GeoPoint(lat=43.66, lon=-1.43)
    summer = _day_length_hours(point, date(2025, 6, 21))
    winter = _day_length_hours(point, date(2025, 12, 21))
    assert winter < summer
    assert 8.0 <= winter <= 10.0


def test_equator_day_length_is_about_twelve_hours() -> None:
    point = GeoPoint(lat=0.0, lon=0.0)
    hours = _day_length_hours(point, date(2025, 3, 20))  # near equinox
    assert 11.5 <= hours <= 12.5


def test_noon_is_daylight_at_midlatitude_summer() -> None:
    point = GeoPoint(lat=43.66, lon=-1.43)
    info = compute_daylight(point, date(2025, 6, 21))
    # ~13:00 local solar time near 0 deg meridian is well within daylight.
    assert info.is_daylight(datetime(2025, 6, 21, 12, 0, tzinfo=UTC)) is True
    # Deep night is outside daylight.
    assert info.is_daylight(datetime(2025, 6, 21, 2, 0, tzinfo=UTC)) is False


# --------------------------------------------------------------------------- #
# Polar edge cases
# --------------------------------------------------------------------------- #
def test_polar_night_is_never_daylight() -> None:
    # High Arctic (Svalbard ~78N) in deep winter -> polar night.
    point = GeoPoint(lat=78.0, lon=15.0)
    on_date = date(2025, 12, 21)
    info = compute_daylight(point, on_date)
    # Empty interval convention: sunrise after sunset.
    assert info.sunrise > info.sunset
    # No timestamp within the day is daylight.
    start = datetime(2025, 12, 21, 0, 0, tzinfo=UTC)
    for hour in range(24):
        assert info.is_daylight(start + timedelta(hours=hour)) is False


def test_midnight_sun_is_always_daylight() -> None:
    # High Arctic (Svalbard ~78N) at the summer solstice -> midnight sun.
    point = GeoPoint(lat=78.0, lon=15.0)
    on_date = date(2025, 6, 21)
    info = compute_daylight(point, on_date)
    # Full-day interval convention.
    assert info.sunrise <= info.sunset
    start = datetime(2025, 6, 21, 0, 0, tzinfo=UTC)
    for hour in range(24):
        assert info.is_daylight(start + timedelta(hours=hour)) is True


def test_polar_cases_do_not_raise_for_extreme_latitudes() -> None:
    for lat in (-90.0, -80.0, 80.0, 90.0):
        point = GeoPoint(lat=lat, lon=0.0)
        # Neither solstice should raise for any extreme latitude.
        compute_daylight(point, date(2025, 6, 21))
        compute_daylight(point, date(2025, 12, 21))
