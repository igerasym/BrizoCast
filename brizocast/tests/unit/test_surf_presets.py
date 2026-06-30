"""Unit tests for bundled static surf presets and compass conversion (Req 4.1, 4.2, 4.9).

These are pure (no I/O) checks that:

* the generic fallback preset always exists and carries the full surf parameter
  shape;
* ``static_presets_for_region`` returns the generic fallback for an unknown
  region and an empty list when ``region=None`` (no region-specific statics);
* ``first_default_for_region`` always yields a preset (the generic fallback for
  an unknown/None region), so effective-conditions resolution never fails
  (Req 4.9);
* the compass ↔ degrees bridge used to store/read directions is correct.
"""

from __future__ import annotations

import pytest

from brizocast.activities.surf.conditions import SurfConditions
from brizocast.activities.surf.directions import (
    compass_to_degrees,
    degrees_to_compass,
)
from brizocast.activities.surf.presets import (
    GENERIC_REGION,
    first_default_for_region,
    static_preset_regions,
    static_presets_for_region,
)
from brizocast.core.errors import DomainValidationError

pytestmark = pytest.mark.unit


def test_no_region_specific_static_presets() -> None:
    """No hardcoded region presets remain — presets are managed via admin."""
    assert static_preset_regions() == []


def test_region_none_returns_empty_list() -> None:
    """A ``None`` region returns an empty list (no region-specific statics)."""
    presets = static_presets_for_region(None)
    assert presets == []


def test_unknown_region_falls_back_to_generic_defaults() -> None:
    """An unrecognised region yields the generic fallback presets."""
    presets = static_presets_for_region("Atlantis")
    assert presets
    assert all(p.region == GENERIC_REGION for p in presets)


def test_generic_fallback_defines_full_surf_parameter_shape() -> None:
    """The generic fallback defines wave band, period, wind, and directions."""
    preset = first_default_for_region(None)
    params = preset.params
    assert params.min_wave_m <= params.max_wave_m
    assert params.min_period_s > 0.0
    assert params.max_wind_kmh > 0.0
    assert params.preferred_wind_dir_deg is not None
    assert params.preferred_swell_dir_deg is not None


def test_first_default_always_returns_a_preset() -> None:
    """``first_default_for_region`` never returns ``None`` (Req 4.9)."""
    assert first_default_for_region("Atlantis").region == GENERIC_REGION
    assert first_default_for_region(None) is not None
    assert first_default_for_region(None).region == GENERIC_REGION


def test_default_preset_projects_to_surf_conditions() -> None:
    """A default maps field-for-field onto SurfConditions the scorer consumes."""
    default = first_default_for_region(None)
    conditions = default.to_conditions()
    assert isinstance(conditions, SurfConditions)
    assert conditions.min_wave_m == default.params.min_wave_m
    assert conditions.max_wave_m == default.params.max_wave_m
    assert conditions.preferred_wind_dir_deg == default.params.preferred_wind_dir_deg
    # Custom-only fields default off / none when sourced from a preset.
    assert conditions.daylight_only is False
    assert conditions.tide_preference is None


@pytest.mark.parametrize(
    ("point", "degrees"),
    [("N", 0.0), ("E", 90.0), ("S", 180.0), ("W", 270.0), ("NW", 315.0), ("ENE", 67.5)],
)
def test_compass_to_degrees_known_points(point: str, degrees: float) -> None:
    """Known compass points convert to their canonical bearing."""
    assert compass_to_degrees(point) == degrees
    # Case- and whitespace-insensitive.
    assert compass_to_degrees(f"  {point.lower()} ") == degrees


def test_compass_conversion_none_passthrough() -> None:
    """``None`` direction round-trips as ``None`` in both directions."""
    assert compass_to_degrees(None) is None
    assert degrees_to_compass(None) is None


def test_unknown_compass_point_raises() -> None:
    """A non-empty, unrecognised compass point is rejected."""
    with pytest.raises(DomainValidationError):
        compass_to_degrees("XYZ")


@pytest.mark.parametrize(
    ("degrees", "expected"),
    [(0.0, "N"), (44.0, "NE"), (90.0, "E"), (200.0, "SSW"), (315.0, "NW"), (360.0, "N"), (361.0, "N")],
)
def test_degrees_to_compass_snaps_to_nearest_point(degrees: float, expected: str) -> None:
    """Degrees snap to the nearest 16-point compass name, wrapping modulo 360."""
    assert degrees_to_compass(degrees) == expected
