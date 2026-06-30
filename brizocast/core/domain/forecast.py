"""Forecast value objects (pure, no I/O).

Defines the time-series value objects produced by a ``ForecastProvider`` and
consumed by the scorer and notification engine:

- :class:`ForecastWindow` — a ``[start, end]`` interval with a deterministic
  :meth:`ForecastWindow.key` used as the stable identity for anti-spam dedup.
- :class:`ForecastStep` — a single forecast time step carrying wave, swell, and
  wind values (Requirement 6.4: every step is complete).
- :class:`Forecast` — a spot's ordered series of steps.

These are framework-free Pydantic models with no dependency on persistence,
Telegram, or any provider.

Requirements covered: 6.4 (forecast step completeness).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

# Compass bearing bounds (degrees), inclusive of the 360 wrap-around endpoint.
DIRECTION_MIN = 0.0
DIRECTION_MAX = 360.0


class ForecastWindow(BaseModel):
    """An immutable forecast time interval ``[start, end]``.

    The :meth:`key` method yields a stable, deterministic identity string used
    by the notification engine to deduplicate alerts for the same window
    (anti-spam). The same ``(start, end)`` pair always produces the same key,
    independent of the input timezone representation.
    """

    model_config = ConfigDict(frozen=True)

    start: datetime = Field(description="Inclusive start of the forecast window.")
    end: datetime = Field(description="Inclusive end of the forecast window.")

    @staticmethod
    def _to_utc(value: datetime) -> datetime:
        """Normalize a datetime to UTC, treating naive values as already-UTC."""

        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def key(self) -> str:
        """Return a stable identity string, e.g. ``"2025-06-01T06:00Z/3h"``.

        The start is normalized to UTC and rendered at minute precision with a
        ``Z`` suffix; the duration is rendered in the largest whole unit among
        hours (``h``), minutes (``m``), or seconds (``s``). Equal windows always
        produce equal keys regardless of the timezone they were supplied in.
        """

        start_utc = self._to_utc(self.start)
        stamp = start_utc.strftime("%Y-%m-%dT%H:%MZ")

        total_seconds = int((self.end - self.start).total_seconds())
        if total_seconds % 3600 == 0:
            duration = f"{total_seconds // 3600}h"
        elif total_seconds % 60 == 0:
            duration = f"{total_seconds // 60}m"
        else:
            duration = f"{total_seconds}s"

        return f"{stamp}/{duration}"


class ForecastStep(BaseModel):
    """A single forecast time step with complete wave, swell, and wind data.

    Every step carries a timestamp plus wave height, swell period, swell
    direction, wind speed, and wind direction (Req 6.4). Magnitudes are
    non-negative; directions are constrained to ``[0, 360]`` degrees.
    """

    model_config = ConfigDict(frozen=True)

    timestamp: datetime = Field(description="Instant the step applies to.")
    wave_height_m: float = Field(ge=0.0, description="Significant wave height in metres.")
    swell_period_s: float = Field(ge=0.0, description="Swell period in seconds.")
    swell_direction_deg: float = Field(
        ge=DIRECTION_MIN, le=DIRECTION_MAX, description="Swell direction in degrees, 0..360."
    )
    wind_speed_kmh: float = Field(ge=0.0, description="Wind speed in km/h.")
    wind_direction_deg: float = Field(
        ge=DIRECTION_MIN, le=DIRECTION_MAX, description="Wind direction in degrees, 0..360."
    )
    # Optional weather fields — populated when the provider supplies them.
    temperature_c: float | None = Field(default=None, description="Air temperature in °C.")
    weather_code: int | None = Field(default=None, description="WMO weather code.")


class Forecast(BaseModel):
    """A surf spot's ordered time-series of :class:`ForecastStep` values."""

    model_config = ConfigDict(frozen=True)

    spot_id: str = Field(min_length=1, description="Stable spot key the forecast belongs to.")
    steps: list[ForecastStep] = Field(
        default_factory=list, description="Ordered forecast time steps."
    )
