"""Compass-bearing helpers bridging stored strings and domain degrees (pure).

The persistence layer stores preferred/acceptable wind and swell directions as
short 16-point compass strings (``presets.preferred_wind_dir`` and
``custom_conditions.acceptable_wind_dir`` are ``String(16)`` columns, task 1.4),
whereas the pure domain — :class:`~brizocast.core.domain.conditions.PresetParams`
and :class:`~brizocast.activities.surf.conditions.SurfConditions` — and the
``SurfScorer``'s ``direction_match`` curve work in **degrees** (``0..360``).

This module is the single, pure, well-tested bridge between the two
representations, used by :mod:`brizocast.services.preset_service` when mapping a
stored preset / custom condition onto :class:`SurfConditions` and back:

* :func:`compass_to_degrees` — ``"NW"`` → ``315.0`` (``None`` → ``None``).
* :func:`degrees_to_compass` — ``315.0`` → ``"NW"`` (``None`` → ``None``).

Quantisation note
-----------------
The column shape only models the 16 standard compass points, so a degrees →
compass → degrees round-trip snaps to the nearest 22.5° point and is therefore
**lossy** for arbitrary bearings. Callers that need exact bearings should keep
the degree value; the stored form is a 16-point approximation by design.
"""

from __future__ import annotations

from typing import Final

from brizocast.core.errors import DomainValidationError

__all__ = [
    "COMPASS_POINTS",
    "compass_to_degrees",
    "degrees_to_compass",
]

# The 16 standard compass points in clockwise order from North, paired with
# their bearing in degrees. Order matters: the index is the 22.5°-sector number.
COMPASS_POINTS: Final[tuple[tuple[str, float], ...]] = (
    ("N", 0.0),
    ("NNE", 22.5),
    ("NE", 45.0),
    ("ENE", 67.5),
    ("E", 90.0),
    ("ESE", 112.5),
    ("SE", 135.0),
    ("SSE", 157.5),
    ("S", 180.0),
    ("SSW", 202.5),
    ("SW", 225.0),
    ("WSW", 247.5),
    ("W", 270.0),
    ("WNW", 292.5),
    ("NW", 315.0),
    ("NNW", 337.5),
)

# Degrees per compass sector (360 / 16).
_SECTOR_DEG: Final[float] = 22.5

# Lookup of the canonical bearing for each compass point name (upper-cased key).
_NAME_TO_DEG: Final[dict[str, float]] = {name: deg for name, deg in COMPASS_POINTS}


def compass_to_degrees(point: str | None) -> float | None:
    """Return the bearing in degrees for a 16-point compass ``point``.

    Args:
        point: A compass point such as ``"N"`` or ``"WNW"`` (case- and
            whitespace-insensitive), or ``None`` when no direction is set.

    Returns:
        The bearing in degrees (``0..337.5``), or ``None`` when ``point`` is
        ``None``.

    Raises:
        DomainValidationError: If ``point`` is a non-empty string that is not a
            recognised compass point.
    """
    if point is None:
        return None
    key = point.strip().upper()
    try:
        return _NAME_TO_DEG[key]
    except KeyError as exc:
        raise DomainValidationError(
            f"unknown compass point {point!r}; expected one of "
            f"{', '.join(name for name, _ in COMPASS_POINTS)}"
        ) from exc


def degrees_to_compass(degrees: float | None) -> str | None:
    """Return the nearest 16-point compass name for a bearing in ``degrees``.

    The bearing is normalised into ``[0, 360)`` and snapped to the nearest
    22.5° sector, so the result is one of the 16 :data:`COMPASS_POINTS` names.
    This is the lossy inverse of :func:`compass_to_degrees` (see the module
    note).

    Args:
        degrees: A bearing in degrees (any real value; wrapped modulo 360), or
            ``None`` when no direction is set.

    Returns:
        The nearest compass point name, or ``None`` when ``degrees`` is ``None``.
    """
    if degrees is None:
        return None
    normalised = degrees % 360.0
    sector = int(normalised / _SECTOR_DEG + 0.5) % len(COMPASS_POINTS)
    return COMPASS_POINTS[sector][0]
