"""Bundled static Default_Presets for the surf activity (Req 4.1, 4.2, 4.9).

The System ships one or more :term:`Default_Preset`s for each supported region
(Req 4.1). Each preset defines the full surf parameter shape — minimum/maximum
wave height, minimum swell period, maximum wind, and preferred wind/swell
directions (Req 4.2) — carried as a :class:`~brizocast.core.domain.conditions.PresetParams`
so a static default and an AI-generated default are **interchangeable** (they
share one shape and persist in one table, Req 16.10/19.4/19.5). This module is
pure data + lookups with no I/O and no provenance assumptions.

Lookups
-------
* :func:`static_presets_for_region` — the defaults for a region (all defaults
  when ``region`` is ``None``; the generic fallback for an unknown region).
* :func:`first_default_for_region` — a region's **first** default, used as the
  last-resort effective conditions when a subscription has neither custom
  conditions nor a selected preset (Req 4.9). It always returns a preset
  (falling back to the generic default), so resolution never fails.

Regions covered here mirror the seed surf-spot dataset
(``storage/spots/surf_spots.json``): Peniche, Ericeira, Basque Country,
Hossegor, Landes, and Donegal, plus a generic all-round fallback. Directions
are stored in degrees (offshore wind / dominant swell for each coast) so they
feed the scorer's ``direction_match`` curve directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from brizocast.activities.surf.conditions import SurfConditions, TidePreference
from brizocast.core.domain.conditions import PresetParams

__all__ = [
    "GENERIC_REGION",
    "DefaultPreset",
    "all_static_presets",
    "first_default_for_region",
    "static_preset_regions",
    "static_presets_for_region",
]

# Sentinel region name for the all-round fallback presets used when a region is
# unknown or unspecified.
GENERIC_REGION: Final[str] = "Default"


@dataclass(frozen=True)
class DefaultPreset:
    """A bundled static default preset: a region, a name, and its parameters.

    Wraps a :class:`~brizocast.core.domain.conditions.PresetParams` (the shared
    surf parameter shape) together with the human-facing ``name`` shown in the
    ``/presets`` list and the ``region`` it applies to. :meth:`to_conditions`
    projects it onto a :class:`SurfConditions` the scorer can consume.
    """

    region: str
    name: str
    params: PresetParams

    def to_conditions(
        self,
        *,
        daylight_only: bool = False,
        tide_preference: TidePreference | None = None,
    ) -> SurfConditions:
        """Project this default onto a :class:`SurfConditions` (Req 4.7, 4.9).

        The preset parameters map field-for-field; the Custom_Conditions-only
        extras (``daylight_only`` and ``tide_preference``) default to off/none
        and may be supplied by the caller when a default is adopted with those
        preferences.
        """
        p = self.params
        return SurfConditions(
            min_wave_m=p.min_wave_m,
            max_wave_m=p.max_wave_m,
            min_period_s=p.min_period_s,
            max_wind_kmh=p.max_wind_kmh,
            preferred_wind_dir_deg=p.preferred_wind_dir_deg,
            preferred_swell_dir_deg=p.preferred_swell_dir_deg,
            tide_preference=tide_preference,
            daylight_only=daylight_only,
        )


# --------------------------------------------------------------------------- #
# Bundled static defaults, keyed by region. Each region has one or more presets
# (Req 4.1); the first entry is the region's default-of-record (Req 4.9).
# Directions are degrees: preferred wind ≈ offshore for the coast's aspect,
# preferred swell ≈ the dominant groundswell direction.
# --------------------------------------------------------------------------- #
_STATIC_PRESETS: Final[dict[str, tuple[DefaultPreset, ...]]] = {
    GENERIC_REGION: (
        DefaultPreset(
            region=GENERIC_REGION,
            name="All-Round Balanced",
            params=PresetParams(
                min_wave_m=0.8,
                max_wave_m=2.5,
                min_period_s=8.0,
                max_wind_kmh=25.0,
                preferred_wind_dir_deg=90.0,  # E offshore (typical W-facing coast)
                preferred_swell_dir_deg=270.0,  # W
            ),
        ),
    ),
}


def static_preset_regions() -> list[str]:
    """Return the regions that have bundled static defaults (excluding generic).

    The generic fallback region (:data:`GENERIC_REGION`) is omitted because it
    is an implementation fallback rather than a user-facing region.
    """
    return [region for region in _STATIC_PRESETS if region != GENERIC_REGION]


def static_presets_for_region(region: str | None) -> list[DefaultPreset]:
    """Return the bundled default presets for ``region`` (Req 4.1, 4.3).

    Args:
        region: The region to look up. When ``None``, every bundled default
            across all regions is returned (excluding the generic fallback), so
            callers can present the full default catalogue. When the region is
            not recognised, the generic all-round fallback is returned so a
            usable default always exists.

    Returns:
        The matching default presets, newest-to-oldest insertion order
        preserved; the first element is the region's default-of-record.
    """
    if region is None:
        return [
            preset
            for reg, presets in _STATIC_PRESETS.items()
            if reg != GENERIC_REGION
            for preset in presets
        ]
    presets = _STATIC_PRESETS.get(region)
    if presets is None:
        return list(_STATIC_PRESETS[GENERIC_REGION])
    return list(presets)


def first_default_for_region(region: str | None) -> DefaultPreset:
    """Return a region's **first** default preset (Req 4.9).

    Used as the last-resort effective conditions when a subscription has neither
    custom conditions nor a selected preset. Always returns a preset: an unknown
    or ``None`` region falls back to the generic all-round default, so
    resolution never fails.
    """
    presets = static_presets_for_region(region)
    if presets:
        return presets[0]
    # ``region is None`` with no non-generic regions defined, or any other gap:
    # fall back to the generic default, which is guaranteed to exist.
    return _STATIC_PRESETS[GENERIC_REGION][0]


def all_static_presets() -> list[DefaultPreset]:
    """Return every bundled default preset, including the generic fallback."""
    return [preset for presets in _STATIC_PRESETS.values() for preset in presets]
