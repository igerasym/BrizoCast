"""Explainable surf-alert message renderers (pure functions, no I/O).

Renders the human-readable text of a surf alert and assembles the full alert
message (text + inline feedback keyboard) the notification sender dispatches.

An alert must explain *why* a session is worth it and let the user respond:

* :func:`format_alert_text` builds the message text containing the surf score,
  the score category, the surf-spot name, and the forecast window (Req 12.1),
  plus the per-factor breakdown of wave height, swell period, and wind
  contributions produced by the scoring engine (Req 12.2). It reads the score,
  category, window, and breakdown from a :class:`ScoreResult` and the raw
  human-facing values (wave metres, swell period seconds, wind speed/direction)
  from the :class:`ForecastStep` that produced it — the breakdown carries only
  normalized ``[0, 1]`` contributions, so the raw values are supplied
  explicitly to keep the function pure and lossless.
* :func:`build_alert_message` pairs that text with the inline 👍/👎 feedback
  keyboard (Req 12.3) so callers get the complete presentation in one call.

Both functions are pure and presentation-only: no service calls, no
persistence. Feedback persistence is handled by the callback handler (task 7.9)
via the callback-data scheme in :mod:`brizocast.bot.keyboards.feedback`.

Requirements covered: 12.1, 12.2, 12.3 (supports Property 27).
"""

from __future__ import annotations

from datetime import UTC, datetime

from telegram import InlineKeyboardMarkup

from brizocast.bot.keyboards.feedback import build_feedback_keyboard
from brizocast.core.domain.forecast import ForecastStep, ForecastWindow
from brizocast.core.domain.scoring import ScoreResult
from brizocast.core.domain.spot import SurfSpot

__all__ = ["build_alert_message", "format_alert_text"]

# Eight-point compass labels indexed by 45-degree sector, starting at North.
_COMPASS_8 = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

# Percentage scale for rendering a normalized factor contribution.
_PERCENT = 100


def _category_label(result: ScoreResult) -> str:
    """Render a score category as a title-cased word, e.g. ``"Excellent"``."""

    return result.category.name.title()


def _compass(direction_deg: float) -> str:
    """Map a bearing in degrees to its nearest 8-point compass label."""

    sector = int(direction_deg / 45.0 + 0.5) % len(_COMPASS_8)
    return _COMPASS_8[sector]


def _percent(value: float) -> int:
    """Render a normalized ``[0, 1]`` factor value as an integer percentage."""

    return round(value * _PERCENT)


def _format_window(window: ForecastWindow) -> str:
    """Render a forecast window as a readable UTC time range.

    A single-instant window (``start == end``) renders as one timestamp; a true
    interval renders ``start–end``, omitting the repeated date when the window
    starts and ends on the same UTC day. Times are normalized to UTC for
    determinism regardless of the input timezone.
    """

    start = _to_utc(window.start)
    end = _to_utc(window.end)
    start_text = start.strftime("%Y-%m-%d %H:%M")
    if start == end:
        return f"{start_text} UTC"
    if start.date() == end.date():
        return f"{start_text}\u2013{end.strftime('%H:%M')} UTC"
    return f"{start_text}\u2013{end.strftime('%Y-%m-%d %H:%M')} UTC"


def _to_utc(value: datetime) -> datetime:
    """Normalize a datetime to UTC, treating naive values as already-UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _wind_emoji(direction_deg: float) -> str:
    """Return a directional arrow emoji for the wind direction."""
    arrows = ("⬆️", "↗️", "➡️", "↘️", "⬇️", "↙️", "⬅️", "↖️")
    sector = int(direction_deg / 45.0 + 0.5) % len(arrows)
    return arrows[sector]


def _score_bar(score: int) -> str:
    """Visual bar representing score 0-100."""
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def _score_stars(score: int) -> str:
    """★★★★☆ style rating from 0-100 score."""
    stars = round(score / 20)  # 0-5 stars
    return "★" * stars + "☆" * (5 - stars)


def _wave_energy(wave_m: float, period_s: float) -> str:
    """Wave power in kW/m: standard surfer formula P ≈ 0.5 × H² × T."""
    energy = 0.5 * wave_m ** 2 * period_s
    if energy < 10:
        label = "🟢"
    elif energy < 30:
        label = "🟡"
    elif energy < 60:
        label = "🟠"
    else:
        label = "🔴"
    return f"{energy:.0f} kW/m {label}"


def _swell_direction_arrow(direction_deg: float) -> str:
    """Return a directional arrow for swell direction."""
    arrows = ("↓", "↙", "←", "↖", "↑", "↗", "→", "↘")
    sector = int(direction_deg / 45.0 + 0.5) % len(arrows)
    return arrows[sector]


def _weather_emoji(weather_code: int | None, temperature_c: float | None) -> str:
    """Return a weather condition emoji + temperature string.

    Uses WMO weather code groups:
    0=clear, 1-3=partly cloudy, 45-48=fog, 51-67=rain/drizzle,
    71-77=snow, 80-82=showers, 95-99=thunderstorm.
    """
    if weather_code is None:
        emoji = "🌤"
    elif weather_code == 0:
        emoji = "☀️"
    elif weather_code <= 3:
        emoji = "🌤"
    elif weather_code <= 48:
        emoji = "🌫"
    elif weather_code <= 67:
        emoji = "🌧"
    elif weather_code <= 77:
        emoji = "🌨"
    elif weather_code <= 82:
        emoji = "🌦"
    else:
        emoji = "⛈"
    temp = f" {temperature_c:.0f}°C" if temperature_c is not None else ""
    return f"{emoji}{temp}"


def _wind_state(wind_dir_deg: float, offshore_dir_deg: float | None) -> str:
    """Determine wind state relative to the spot's offshore direction.

    offshore_dir_deg: the ideal wind direction (blowing away from shore).
    Returns: offshore 🟢 / cross-off 🟡 / cross-on 🟠 / onshore 🔴
    """
    if offshore_dir_deg is None:
        return ""
    diff = abs((wind_dir_deg - offshore_dir_deg + 180) % 360 - 180)
    if diff <= 45:
        return "offshore 🟢"
    elif diff <= 90:
        return "cross-off 🟡"
    elif diff <= 135:
        return "cross-on 🟠"
    else:
        return "onshore 🔴"


def _google_maps_url(lat: float, lon: float) -> str:
    """Return a Google Maps directions URL to the spot."""
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"


def format_alert_text(
    spot: SurfSpot,
    result: ScoreResult,
    step: ForecastStep,
    *,
    offshore_dir_deg: float | None = None,
) -> str:
    """Render a clean, readable surf alert (Req 12.1, 12.2)."""

    wind_dir = _compass(step.wind_direction_deg)
    wind_arrow = _wind_emoji(step.wind_direction_deg)
    swell_arrow = _swell_direction_arrow(step.swell_direction_deg)
    swell_dir = _compass(step.swell_direction_deg)
    stars = _score_stars(result.score)
    category = _category_label(result)
    state = _wind_state(step.wind_direction_deg, offshore_dir_deg)

    # Time
    window_start = _to_utc(result.forecast_window.start)
    time_str = window_start.strftime("%a %d %b · %H:%M UTC")

    # Location line
    location = ""
    if spot.region and spot.country:
        location = f"📍 {spot.region}, {spot.country}"
    elif spot.country:
        location = f"📍 {spot.country}"

    # Google Maps link
    maps_link = _google_maps_url(spot.lat, spot.lon)

    # Build wind line
    wind_line = f"💨 {wind_arrow} {wind_dir} {step.wind_speed_kmh:.0f} km/h"
    if state:
        wind_line += f" · {state}"

    # Weather line (optional)
    weather_line = _weather_emoji(step.weather_code, step.temperature_c)

    parts = [f"🏄 *{spot.name}*"]
    if location:
        parts.append(location)
    parts += [
        "",
        f"{stars}  {result.score}/100 — {category.upper()}",
        "",
        f"🌊 {step.wave_height_m:.1f}m · {swell_arrow}{swell_dir} · {step.swell_period_s:.0f}s",
        f"⚡ {_wave_energy(step.wave_height_m, step.swell_period_s)}",
        wind_line,
        f"🌡 {weather_line}",
        "",
        f"🕐 {time_str}",
        "",
        f"[📍 Directions]({maps_link})",
    ]
    return "\n".join(parts)


def build_alert_message(
    spot: SurfSpot,
    result: ScoreResult,
    step: ForecastStep,
    subscription_id: int,
    *,
    offshore_dir_deg: float | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    text = format_alert_text(spot, result, step, offshore_dir_deg=offshore_dir_deg)
    keyboard = build_feedback_keyboard(
        subscription_id=subscription_id,
        spot_key=spot.spot_key,
        surf_score=result.score,
    )
    return text, keyboard
