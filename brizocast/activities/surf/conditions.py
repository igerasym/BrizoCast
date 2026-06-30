"""Surf condition schema (pure, no I/O).

Defines :class:`SurfConditions`, the surf activity's condition schema consumed
by the :class:`~brizocast.activities.surf.scorer.SurfScorer`. It subclasses the
cross-activity :class:`~brizocast.core.domain.conditions.ConditionsModel` so the
``Scorer`` port and ``Activity`` abstraction can refer to a common base.

The field shape is kept consistent with
:class:`~brizocast.core.domain.conditions.PresetParams` (so a preset maps
directly onto conditions) and extended with the Custom_Conditions-only fields
called out in Requirement 4.5: an optional tide preference and a daylight-only
flag. The two preferred-direction fields cover both the preset wording
("preferred wind/swell direction", Req 4.2) and the custom-conditions wording
("acceptable wind/swell direction", Req 4.5) — they are the same target bearing
the scorer's direction factor matches against.

A ``min_wave_m <= max_wave_m`` validator rejects an inverted wave band
(Req 4.8; supports Property 18).
"""

from __future__ import annotations

from enum import Enum

from pydantic import ConfigDict, Field, model_validator

from brizocast.core.domain.conditions import DIRECTION_MAX, DIRECTION_MIN, ConditionsModel


class TidePreference(str, Enum):
    """Optional preferred tide state for a surf spot.

    Persisted with a subscription's conditions for future tide-aware scoring;
    the MVP scorer does not yet consume it (forecast steps carry no tide data),
    but the value round-trips so it is available to later scoring iterations and
    the alert formatter.
    """

    LOW = "low"
    MID = "mid"
    HIGH = "high"


class SurfConditions(ConditionsModel):
    """Surf scoring conditions: a preset's parameters plus custom-only fields.

    The favourable wave-height band, minimum acceptable swell period, maximum
    acceptable wind speed, and optional preferred wind/swell directions mirror
    :class:`~brizocast.core.domain.conditions.PresetParams`. The optional
    ``tide_preference`` and the ``daylight_only`` flag are the additional
    Custom_Conditions fields from Requirement 4.5.

    Directions are optional: when a preferred direction is unset the scorer
    treats that direction factor as fully satisfied (no penalty).
    """

    model_config = ConfigDict(frozen=True)

    min_wave_m: float = Field(ge=0.0, description="Minimum favourable wave height in metres.")
    max_wave_m: float = Field(ge=0.0, description="Maximum favourable wave height in metres.")
    min_period_s: float = Field(ge=0.0, description="Minimum acceptable swell period in seconds.")
    max_wind_kmh: float = Field(ge=0.0, description="Maximum acceptable wind speed in km/h.")
    preferred_wind_dir_deg: float | None = Field(
        default=None,
        ge=DIRECTION_MIN,
        le=DIRECTION_MAX,
        description="Preferred (offshore) wind direction in degrees, 0..360; None if unset.",
    )
    preferred_swell_dir_deg: float | None = Field(
        default=None,
        ge=DIRECTION_MIN,
        le=DIRECTION_MAX,
        description="Preferred swell direction in degrees, 0..360; None if unset.",
    )
    tide_preference: TidePreference | None = Field(
        default=None,
        description="Optional preferred tide state; None if no preference.",
    )
    daylight_only: bool = Field(
        default=False,
        description="When True, steps outside daylight hours score 0 / Ignore.",
    )

    @model_validator(mode="after")
    def _check_wave_band(self) -> SurfConditions:
        """Reject conditions whose minimum wave height exceeds the maximum."""

        if self.min_wave_m > self.max_wave_m:
            raise ValueError(
                "min_wave_m must be less than or equal to max_wave_m "
                f"(got min={self.min_wave_m}, max={self.max_wave_m})"
            )
        return self
