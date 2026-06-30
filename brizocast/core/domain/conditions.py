"""Shared condition parameter shapes (pure, no I/O).

Defines the cross-activity base type and preset shape that the ports and the AI
provider reference without depending on any concrete activity:

- :class:`ConditionsModel` — the base type for an activity's condition schema.
  The concrete surf schema (``SurfConditions``) is implemented by task 2.7 and
  subclasses this base; ports type their condition argument as
  ``ConditionsModel`` / ``type[ConditionsModel]``.
- :class:`PresetParams` — the shared surf preset parameter shape (min/max wave
  height, min period, max wind, preferred wind/swell direction). Both bundled
  static presets and AI-generated presets use this shape so they are
  interchangeable everywhere (design Req 19.4/19.5/16.10).

Only the shared base and preset shape live here; the surf-specific condition
schema and scoring curves are added by later tasks.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Compass bearing bounds (degrees), inclusive of the 360 wrap-around endpoint.
DIRECTION_MIN = 0.0
DIRECTION_MAX = 360.0


class ConditionsModel(BaseModel):
    """Base type for an activity's condition schema.

    Activity-specific schemas (e.g. ``SurfConditions`` in task 2.7) subclass
    this marker base so that the ``Scorer`` port and ``Activity`` abstraction can
    refer to a common ``ConditionsModel`` / ``type[ConditionsModel]`` type
    without importing any concrete activity package.
    """

    model_config = ConfigDict()


class PresetParams(BaseModel):
    """The shared surf preset parameter shape.

    Carries the parameters that define a surf preset: the favourable wave-height
    band, minimum acceptable swell period, maximum acceptable wind speed, and
    optional preferred wind and swell directions. Static and AI-generated
    presets share this exact shape so they remain interchangeable across the
    system.
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
    min_alert_score: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Minimum composite score (0-100) required to fire an alert. "
            "Region-specific: ~45 for Baltic/weak-surf regions, ~60-70 for consistent regions. "
            "None = use system default."
        ),
    )

    @model_validator(mode="after")
    def _check_wave_band(self) -> PresetParams:
        """Reject a preset whose minimum wave height exceeds its maximum."""

        if self.min_wave_m > self.max_wave_m:
            raise ValueError(
                "min_wave_m must be less than or equal to max_wave_m "
                f"(got min={self.min_wave_m}, max={self.max_wave_m})"
            )
        return self
