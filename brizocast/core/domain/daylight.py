"""Daylight value object and sunrise/sunset computation (pure, no I/O).

Provides the :class:`DaylightInfo` value object that the surf scorer's daylight
gate depends on (design Req 8.8): it holds a ``sunrise``/``sunset`` pair and
answers :meth:`DaylightInfo.is_daylight` for a given timestamp.

It also provides :func:`compute_daylight`, a pure function that derives a
:class:`DaylightInfo` (sunrise/sunset as UTC datetimes) for a latitude,
longitude, and calendar date using the standard "sunrise equation" solar
position algorithm implemented with the standard-library :mod:`math` only — no
third-party dependency. The computation is approximate (accurate to a couple of
minutes) which is more than sufficient for the daylight-only scoring gate.

Polar edge cases are handled without raising (see :func:`compute_daylight`):

* **Polar night** (the sun never rises on the given day): an *empty* daylight
  interval is returned (``sunrise`` set to the end of the day, ``sunset`` to the
  start), so :meth:`DaylightInfo.is_daylight` is ``False`` for every timestamp.
* **Midnight sun** (the sun never sets on the given day): a *full-day* interval
  is returned (``sunrise`` at the start of the day, ``sunset`` at the start of
  the next day), so :meth:`DaylightInfo.is_daylight` is ``True`` throughout.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from brizocast.core.domain.geo import GeoPoint


class DaylightInfo(BaseModel):
    """Daylight bounds for a location and day.

    ``sunrise`` and ``sunset`` delimit the daylight interval. :meth:`is_daylight`
    reports whether a timestamp falls within ``[sunrise, sunset]``; all values
    are normalized to UTC for comparison so naive and aware datetimes compare
    consistently.
    """

    model_config = ConfigDict(frozen=True)

    sunrise: datetime = Field(description="Sunrise instant for the location/day.")
    sunset: datetime = Field(description="Sunset instant for the location/day.")

    @staticmethod
    def _to_utc(value: datetime) -> datetime:
        """Normalize a datetime to UTC, treating naive values as already-UTC."""

        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def is_daylight(self, timestamp: datetime) -> bool:
        """Return ``True`` when ``timestamp`` falls within ``[sunrise, sunset]``."""

        ts = self._to_utc(timestamp)
        return self._to_utc(self.sunrise) <= ts <= self._to_utc(self.sunset)


# --------------------------------------------------------------------------- #
# Sunrise/sunset computation (standard sunrise equation, math-only)
# --------------------------------------------------------------------------- #

# Julian Date of the J2000.0 epoch (2000-01-01 12:00 UTC).
_J2000 = 2451545.0
# Julian Date of the Unix epoch (1970-01-01 00:00 UTC).
_JD_UNIX_EPOCH = 2440587.5
# Mean obliquity of the ecliptic (Earth's axial tilt), degrees.
_OBLIQUITY_DEG = 23.4397
# Standard solar altitude at sunrise/sunset (centre of the sun ~50' below
# horizon: refraction + solar disc), degrees.
_SUN_ALTITUDE_DEG = -0.833
# Argument of perihelion of Earth's orbit, degrees.
_PERIHELION_DEG = 102.9372


def _julian_to_datetime(julian_date: float) -> datetime:
    """Convert a Julian Date to a timezone-aware UTC :class:`datetime`."""

    unix_seconds = (julian_date - _JD_UNIX_EPOCH) * 86400.0
    return datetime.fromtimestamp(unix_seconds, tz=UTC)


def compute_daylight(point: GeoPoint, on_date: date) -> DaylightInfo:
    """Compute the :class:`DaylightInfo` for ``point`` on ``on_date`` (UTC).

    Implements the standard "sunrise equation": it computes the solar mean
    anomaly, the equation of centre, the ecliptic longitude, the solar transit
    (local solar noon), the sun's declination, and the hour angle, then derives
    sunrise and sunset as the transit minus/plus the hour angle. Sunrise and
    sunset are returned as timezone-aware UTC datetimes.

    Polar edge cases never raise: a day with no sunrise (polar night) yields an
    empty daylight interval (``is_daylight`` always ``False``) and a day with no
    sunset (midnight sun) yields a full-day interval (``is_daylight`` always
    ``True``). See the module docstring for the convention.
    """

    return _compute_daylight(point.lat, point.lon, on_date)


def _compute_daylight(lat: float, lon: float, on_date: date) -> DaylightInfo:
    """Sunrise-equation core operating on raw latitude/longitude floats."""

    # West longitude is positive in the sunrise equation's conventions.
    west_longitude = -lon

    # Anchor on the Julian Date at 12:00 UTC of the requested date so that the
    # computed solar noon lands on the correct calendar day across longitudes.
    julian_noon = float(on_date.toordinal()) + 1721425.0

    # Number of days since J2000.0 for the mean solar noon nearest this date at
    # this longitude (0.0009 ~ leap-second/terrestrial-time correction).
    n = round(julian_noon - _J2000 - 0.0009 - west_longitude / 360.0)

    # Julian Date of mean solar noon at this longitude.
    mean_solar_noon = _J2000 + 0.0009 + west_longitude / 360.0 + n
    days_since_j2000 = mean_solar_noon - _J2000

    # Solar mean anomaly (degrees) and its radians form.
    mean_anomaly_deg = (357.5291 + 0.98560028 * days_since_j2000) % 360.0
    mean_anomaly = math.radians(mean_anomaly_deg)

    # Equation of the centre (degrees).
    center = (
        1.9148 * math.sin(mean_anomaly)
        + 0.0200 * math.sin(2.0 * mean_anomaly)
        + 0.0003 * math.sin(3.0 * mean_anomaly)
    )

    # Ecliptic longitude of the sun (degrees) and its radians form.
    ecliptic_longitude_deg = (mean_anomaly_deg + center + 180.0 + _PERIHELION_DEG) % 360.0
    ecliptic_longitude = math.radians(ecliptic_longitude_deg)

    # Julian Date of solar transit (local solar noon).
    solar_transit = (
        mean_solar_noon
        + 0.0053 * math.sin(mean_anomaly)
        - 0.0069 * math.sin(2.0 * ecliptic_longitude)
    )

    # Declination of the sun.
    sin_declination = math.sin(ecliptic_longitude) * math.sin(math.radians(_OBLIQUITY_DEG))
    declination = math.asin(sin_declination)

    # Hour angle: cos(omega0). Outside [-1, 1] means the sun never crosses the
    # sunrise/sunset altitude on this day (polar night or midnight sun).
    lat_rad = math.radians(lat)
    cos_hour_angle = (
        math.sin(math.radians(_SUN_ALTITUDE_DEG)) - math.sin(lat_rad) * sin_declination
    ) / (math.cos(lat_rad) * math.cos(declination))

    day_start = datetime(on_date.year, on_date.month, on_date.day, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)

    if cos_hour_angle > 1.0:
        # Sun stays below the sunrise altitude all day -> polar night.
        # Empty interval (sunrise after sunset) -> is_daylight always False.
        return DaylightInfo(sunrise=day_end, sunset=day_start)
    if cos_hour_angle < -1.0:
        # Sun stays above the sunset altitude all day -> midnight sun.
        # Full-day interval -> is_daylight always True.
        return DaylightInfo(sunrise=day_start, sunset=day_end)

    hour_angle_deg = math.degrees(math.acos(cos_hour_angle))
    sunrise_jd = solar_transit - hour_angle_deg / 360.0
    sunset_jd = solar_transit + hour_angle_deg / 360.0

    return DaylightInfo(
        sunrise=_julian_to_datetime(sunrise_jd),
        sunset=_julian_to_datetime(sunset_jd),
    )
