"""Unit tests for the pure domain value objects (task 2.1).

Covers instantiation, field validation (out-of-range latitude/longitude and
direction rejection), and the deterministic identity of ``ForecastWindow.key``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from brizocast.core.domain import (
    ConditionsModel,
    DaylightInfo,
    FactorContribution,
    Forecast,
    ForecastStep,
    ForecastWindow,
    GeoCandidate,
    GeoPoint,
    PresetParams,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# GeoPoint / GeoCandidate
# --------------------------------------------------------------------------- #
def test_geopoint_accepts_in_range_coordinates() -> None:
    point = GeoPoint(lat=43.5, lon=-1.5)
    assert (point.lat, point.lon) == (43.5, -1.5)


def test_geopoint_accepts_boundary_values() -> None:
    assert GeoPoint(lat=90.0, lon=180.0).lat == 90.0
    assert GeoPoint(lat=-90.0, lon=-180.0).lon == -180.0


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (90.1, 0.0),
        (-90.1, 0.0),
        (0.0, 180.1),
        (0.0, -180.1),
    ],
)
def test_geopoint_rejects_out_of_range(lat: float, lon: float) -> None:
    with pytest.raises(ValidationError):
        GeoPoint(lat=lat, lon=lon)


def test_geopoint_is_frozen() -> None:
    point = GeoPoint(lat=0.0, lon=0.0)
    with pytest.raises(ValidationError):
        point.lat = 10.0


def test_geocandidate_optional_fields_default_to_none() -> None:
    candidate = GeoCandidate(name="Hossegor", lat=43.66, lon=-1.4)
    assert candidate.city is None and candidate.country is None


def test_geocandidate_to_point_roundtrips_coordinates() -> None:
    candidate = GeoCandidate(name="Ericeira", lat=38.96, lon=-9.42, country="Portugal")
    assert candidate.to_point() == GeoPoint(lat=38.96, lon=-9.42)


def test_geocandidate_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        GeoCandidate(name="", lat=0.0, lon=0.0)


def test_geocandidate_rejects_out_of_range_coordinates() -> None:
    with pytest.raises(ValidationError):
        GeoCandidate(name="bad", lat=120.0, lon=0.0)


# --------------------------------------------------------------------------- #
# ForecastStep / Forecast
# --------------------------------------------------------------------------- #
def _valid_step(**overrides: object) -> ForecastStep:
    base: dict[str, object] = {
        "timestamp": datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
        "wave_height_m": 1.5,
        "swell_period_s": 12.0,
        "swell_direction_deg": 270.0,
        "wind_speed_kmh": 10.0,
        "wind_direction_deg": 90.0,
    }
    base.update(overrides)
    return ForecastStep(**base)  # type: ignore[arg-type]


def test_forecast_step_accepts_complete_valid_data() -> None:
    step = _valid_step()
    assert step.wave_height_m == 1.5
    assert step.swell_direction_deg == 270.0


@pytest.mark.parametrize("direction", [-0.1, 360.1, 400.0])
def test_forecast_step_rejects_out_of_range_swell_direction(direction: float) -> None:
    with pytest.raises(ValidationError):
        _valid_step(swell_direction_deg=direction)


@pytest.mark.parametrize("direction", [-1.0, 360.5])
def test_forecast_step_rejects_out_of_range_wind_direction(direction: float) -> None:
    with pytest.raises(ValidationError):
        _valid_step(wind_direction_deg=direction)


@pytest.mark.parametrize("field", ["wave_height_m", "swell_period_s", "wind_speed_kmh"])
def test_forecast_step_rejects_negative_magnitudes(field: str) -> None:
    with pytest.raises(ValidationError):
        _valid_step(**{field: -1.0})


def test_forecast_step_accepts_direction_boundaries() -> None:
    assert _valid_step(swell_direction_deg=0.0).swell_direction_deg == 0.0
    assert _valid_step(wind_direction_deg=360.0).wind_direction_deg == 360.0


def test_forecast_defaults_to_empty_steps() -> None:
    assert Forecast(spot_id="fr-hossegor").steps == []


def test_forecast_holds_ordered_steps() -> None:
    forecast = Forecast(spot_id="fr-hossegor", steps=[_valid_step()])
    assert forecast.steps[0].wave_height_m == 1.5


def test_forecast_rejects_empty_spot_id() -> None:
    with pytest.raises(ValidationError):
        Forecast(spot_id="")


# --------------------------------------------------------------------------- #
# ForecastWindow.key() determinism
# --------------------------------------------------------------------------- #
def test_forecast_window_key_matches_documented_format() -> None:
    window = ForecastWindow(
        start=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
        end=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
    )
    assert window.key() == "2025-06-01T06:00Z/3h"


def test_forecast_window_key_is_deterministic() -> None:
    start = datetime(2025, 6, 1, 6, 0, tzinfo=UTC)
    end = datetime(2025, 6, 1, 9, 0, tzinfo=UTC)
    first = ForecastWindow(start=start, end=end)
    second = ForecastWindow(start=start, end=end)
    assert first.key() == second.key()


def test_forecast_window_key_is_timezone_independent() -> None:
    # 06:00 UTC and 08:00 in UTC+2 are the same instant -> identical key.
    utc_window = ForecastWindow(
        start=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
        end=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
    )
    plus_two = timezone(timedelta(hours=2))
    offset_window = ForecastWindow(
        start=datetime(2025, 6, 1, 8, 0, tzinfo=plus_two),
        end=datetime(2025, 6, 1, 11, 0, tzinfo=plus_two),
    )
    assert utc_window.key() == offset_window.key()


def test_forecast_window_key_naive_treated_as_utc() -> None:
    naive = ForecastWindow(
        start=datetime(2025, 6, 1, 6, 0),
        end=datetime(2025, 6, 1, 9, 0),
    )
    assert naive.key() == "2025-06-01T06:00Z/3h"


def test_forecast_window_key_uses_minutes_then_seconds_units() -> None:
    minute_window = ForecastWindow(
        start=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
        end=datetime(2025, 6, 1, 6, 90 // 2, tzinfo=UTC),  # 45 minutes
    )
    assert minute_window.key() == "2025-06-01T06:00Z/45m"

    second_window = ForecastWindow(
        start=datetime(2025, 6, 1, 6, 0, 0, tzinfo=UTC),
        end=datetime(2025, 6, 1, 6, 0, 30, tzinfo=UTC),  # 30 seconds
    )
    assert second_window.key() == "2025-06-01T06:00Z/30s"


# --------------------------------------------------------------------------- #
# FactorContribution
# --------------------------------------------------------------------------- #
def test_factor_contribution_weighted_is_product() -> None:
    contribution = FactorContribution(value=0.8, weight=0.25)
    assert contribution.weighted == pytest.approx(0.2)


@pytest.mark.parametrize(
    ("value", "weight"),
    [(-0.1, 0.5), (1.1, 0.5), (0.5, -0.1), (0.5, 1.1)],
)
def test_factor_contribution_rejects_out_of_unit_range(value: float, weight: float) -> None:
    with pytest.raises(ValidationError):
        FactorContribution(value=value, weight=weight)


# --------------------------------------------------------------------------- #
# PresetParams / ConditionsModel
# --------------------------------------------------------------------------- #
def test_preset_params_accepts_valid_shape() -> None:
    preset = PresetParams(
        min_wave_m=0.8,
        max_wave_m=2.5,
        min_period_s=9.0,
        max_wind_kmh=25.0,
        preferred_wind_dir_deg=90.0,
        preferred_swell_dir_deg=270.0,
    )
    assert preset.max_wave_m == 2.5


def test_preset_params_optional_directions_default_to_none() -> None:
    preset = PresetParams(min_wave_m=0.5, max_wave_m=2.0, min_period_s=8.0, max_wind_kmh=20.0)
    assert preset.preferred_wind_dir_deg is None
    assert preset.preferred_swell_dir_deg is None


def test_preset_params_rejects_min_wave_above_max() -> None:
    with pytest.raises(ValidationError):
        PresetParams(min_wave_m=3.0, max_wave_m=1.0, min_period_s=8.0, max_wind_kmh=20.0)


def test_preset_params_rejects_out_of_range_direction() -> None:
    with pytest.raises(ValidationError):
        PresetParams(
            min_wave_m=0.5,
            max_wave_m=2.0,
            min_period_s=8.0,
            max_wind_kmh=20.0,
            preferred_wind_dir_deg=361.0,
        )


def test_conditions_model_is_subclassable_base() -> None:
    class _SampleConditions(ConditionsModel):
        daylight_only: bool = False

    schema: type[ConditionsModel] = _SampleConditions
    assert issubclass(schema, ConditionsModel)
    assert _SampleConditions(daylight_only=True).daylight_only is True


# --------------------------------------------------------------------------- #
# DaylightInfo
# --------------------------------------------------------------------------- #
def test_daylight_info_reports_inside_and_outside() -> None:
    info = DaylightInfo(
        sunrise=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
        sunset=datetime(2025, 6, 1, 21, 0, tzinfo=UTC),
    )
    assert info.is_daylight(datetime(2025, 6, 1, 12, 0, tzinfo=UTC)) is True
    assert info.is_daylight(datetime(2025, 6, 1, 23, 0, tzinfo=UTC)) is False


def test_daylight_info_boundaries_are_inclusive() -> None:
    sunrise = datetime(2025, 6, 1, 6, 0, tzinfo=UTC)
    sunset = datetime(2025, 6, 1, 21, 0, tzinfo=UTC)
    info = DaylightInfo(sunrise=sunrise, sunset=sunset)
    assert info.is_daylight(sunrise) is True
    assert info.is_daylight(sunset) is True


def test_daylight_info_handles_naive_timestamp_as_utc() -> None:
    info = DaylightInfo(
        sunrise=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
        sunset=datetime(2025, 6, 1, 21, 0, tzinfo=UTC),
    )
    assert info.is_daylight(datetime(2025, 6, 1, 12, 0)) is True
