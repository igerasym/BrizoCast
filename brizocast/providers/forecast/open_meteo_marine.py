"""Open-Meteo Marine forecast provider (the MVP default).

:class:`OpenMeteoMarineProvider` implements the
:class:`~brizocast.core.ports.forecast_provider.ForecastProvider` port using the
free, key-less Open-Meteo APIs (Req 6.2). Wave and swell values come from the
Marine API; wind values come from the Weather/Forecast API. The two hourly
responses are aligned by timestamp and mapped into the domain
:class:`~brizocast.core.domain.forecast.Forecast` / ``ForecastStep`` shape so
every produced step carries wave height, swell period, swell direction, wind
speed, wind direction, and a timestamp (Req 6.4).

On any network, HTTP, or response-parsing failure the provider raises
:class:`~brizocast.core.errors.ProviderRequestError` with
``provider="open_meteo_marine"`` — raw ``httpx`` errors never escape (Req 6.5,
18.2). The caller (``ForecastService``) logs the failure and skips the affected
spot for that scheduler run.

Field-mapping assumptions
-------------------------
The Marine API does not expose a dedicated *primary swell* triple in its free
hourly set, so the total-sea wave fields are mapped onto the domain's swell
fields:

==========================  ==========================================
Domain ``ForecastStep`` field    Open-Meteo source field
==========================  ==========================================
``wave_height_m``           Marine ``hourly.wave_height`` (metres)
``swell_period_s``          Marine ``hourly.wave_period`` (seconds)
``swell_direction_deg``     Marine ``hourly.wave_direction`` (degrees)
``wind_speed_kmh``          Weather ``hourly.wind_speed_10m`` (km/h)
``wind_direction_deg``      Weather ``hourly.wind_direction_10m`` (degrees)
``timestamp``               shared ``hourly.time`` entry (UTC)
==========================  ==========================================

Additional assumptions:

* Both APIs are queried with ``timezone=UTC`` and ``windspeed_unit=kmh`` so the
  timestamps align directly and the wind speed needs no conversion.
* Timestamps returned by Open-Meteo are timezone-naive local-to-UTC strings; we
  attach :data:`datetime.UTC` explicitly.
* Directions are normalised into ``[0, 360)`` via modulo to satisfy the domain
  value-object bounds; a hole in either array (``None``) drops that step.
* A produced step requires *both* a marine sample and a wind sample for the same
  timestamp; timestamps present in only one response are skipped.
* The port receives only ``(lat, lon)``; the resulting ``Forecast.spot_id`` is
  synthesised from the rounded coordinates. Callers that cache per spot key
  (``ForecastService``) treat the provider result as the forecast for those
  coordinates.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final

import httpx

from brizocast.core.domain.forecast import (
    DIRECTION_MAX,
    Forecast,
    ForecastStep,
    ForecastWindow,
)
from brizocast.core.errors import ProviderRequestError
from brizocast.core.logging import get_logger

__all__ = ["OpenMeteoMarineProvider"]

logger = get_logger(__name__, provider="open_meteo_marine")

# Public, key-less Open-Meteo endpoints.
_MARINE_URL: Final[str] = "https://marine-api.open-meteo.com/v1/marine"
_WEATHER_URL: Final[str] = "https://api.open-meteo.com/v1/forecast"

# Hourly variables requested from each API.
_MARINE_HOURLY: Final[str] = "wave_height,wave_direction,wave_period"
_WEATHER_HOURLY: Final[str] = "wind_speed_10m,wind_direction_10m,temperature_2m,weathercode"

# A sane default timeout (seconds) for provider HTTP calls.
_DEFAULT_TIMEOUT_S: Final[float] = 15.0


class OpenMeteoMarineProvider:
    """Default :class:`ForecastProvider` backed by the Open-Meteo APIs.

    Args:
        client: Optional shared :class:`httpx.AsyncClient`. When provided it is
            reused (and never closed) by this provider; when ``None`` a
            short-lived client with a sane timeout is created per request.
    """

    key: str = "open_meteo_marine"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def get_forecast(
        self, lat: float, lon: float, window: ForecastWindow
    ) -> Forecast:
        """Return the :class:`Forecast` for ``(lat, lon)`` over ``window``.

        Raises:
            ProviderRequestError: if the network request fails, the API returns
                an error status, or the response cannot be parsed/mapped.
        """

        start_date = window.start.astimezone(UTC).date().isoformat() if window.start.tzinfo else window.start.date().isoformat()
        end_date = window.end.astimezone(UTC).date().isoformat() if window.end.tzinfo else window.end.date().isoformat()

        marine_params: dict[str, str | float] = {
            "latitude": lat,
            "longitude": lon,
            "hourly": _MARINE_HOURLY,
            "timezone": "UTC",
            "start_date": start_date,
            "end_date": end_date,
        }
        weather_params: dict[str, str | float] = {
            "latitude": lat,
            "longitude": lon,
            "hourly": _WEATHER_HOURLY,
            "windspeed_unit": "kmh",
            "timezone": "UTC",
            "start_date": start_date,
            "end_date": end_date,
        }

        try:
            marine_json, weather_json = await self._fetch(marine_params, weather_params)
            steps = self._map_steps(marine_json, weather_json, window)
        except ProviderRequestError:
            raise
        except httpx.HTTPError as exc:
            logger.warning("Open-Meteo request failed: %s", exc)
            raise ProviderRequestError(
                f"Open-Meteo request failed: {exc}", provider=self.key
            ) from exc
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Open-Meteo response could not be parsed: %s", exc)
            raise ProviderRequestError(
                f"Open-Meteo response could not be parsed: {exc}", provider=self.key
            ) from exc

        spot_id = f"{lat:.4f},{lon:.4f}"
        return Forecast(spot_id=spot_id, steps=steps)

    # -- internals ------------------------------------------------------- #

    async def _fetch(
        self,
        marine_params: dict[str, str | float],
        weather_params: dict[str, str | float],
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Fetch both API payloads concurrently, returning parsed JSON dicts."""

        if self._client is not None:
            return await self._fetch_with(self._client, marine_params, weather_params)

        timeout = httpx.Timeout(_DEFAULT_TIMEOUT_S)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await self._fetch_with(client, marine_params, weather_params)

    @staticmethod
    async def _fetch_with(
        client: httpx.AsyncClient,
        marine_params: dict[str, str | float],
        weather_params: dict[str, str | float],
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Issue the two GET requests on ``client`` and return their JSON."""

        marine_resp, weather_resp = await asyncio.gather(
            client.get(_MARINE_URL, params=marine_params),
            client.get(_WEATHER_URL, params=weather_params),
        )
        marine_resp.raise_for_status()
        weather_resp.raise_for_status()
        marine_json: dict[str, object] = marine_resp.json()
        weather_json: dict[str, object] = weather_resp.json()
        return marine_json, weather_json

    @staticmethod
    def _normalise_direction(value: float) -> float:
        """Fold a bearing into ``[0, 360)`` (and clamp the ``360`` endpoint)."""

        folded = float(value) % DIRECTION_MAX
        # ``x % 360`` already yields [0, 360); guard against float edge cases.
        return 0.0 if folded >= DIRECTION_MAX else folded

    @classmethod
    def _map_steps(
        cls,
        marine_json: dict[str, object],
        weather_json: dict[str, object],
        window: ForecastWindow,
    ) -> list[ForecastStep]:
        """Align the two hourly responses by timestamp into ``ForecastStep``s."""

        marine_hourly = cls._hourly(marine_json)
        weather_hourly = cls._hourly(weather_json)

        marine_times = cls._str_list(marine_hourly["time"])
        wave_height = cls._float_list(marine_hourly["wave_height"])
        wave_period = cls._float_list(marine_hourly["wave_period"])
        wave_direction = cls._float_list(marine_hourly["wave_direction"])

        weather_times = cls._str_list(weather_hourly["time"])
        wind_speed = cls._float_list(weather_hourly["wind_speed_10m"])
        wind_direction = cls._float_list(weather_hourly["wind_direction_10m"])
        temperature = cls._float_list(weather_hourly.get("temperature_2m", []))
        weather_codes = cls._float_list(weather_hourly.get("weathercode", []))

        # Index the weather series by timestamp for O(1) alignment.
        weather_by_time: dict[str, tuple[float | None, float | None, float | None, float | None]] = {
            ts: (
                wind_speed[i] if i < len(wind_speed) else None,
                wind_direction[i] if i < len(wind_direction) else None,
                temperature[i] if i < len(temperature) else None,
                weather_codes[i] if i < len(weather_codes) else None,
            )
            for i, ts in enumerate(weather_times)
        }

        start_utc = cls._to_utc(window.start)
        end_utc = cls._to_utc(window.end)

        steps: list[ForecastStep] = []
        for i, ts in enumerate(marine_times):
            weather = weather_by_time.get(ts)
            if weather is None:
                continue
            wind_spd, wind_dir, temp, wcode = weather
            wh = wave_height[i] if i < len(wave_height) else None
            wp = wave_period[i] if i < len(wave_period) else None
            wd = wave_direction[i] if i < len(wave_direction) else None
            if None in (wh, wp, wd, wind_spd, wind_dir):
                continue

            stamp = cls._parse_timestamp(ts)
            if not (start_utc <= stamp <= end_utc):
                continue

            steps.append(
                ForecastStep(
                    timestamp=stamp,
                    wave_height_m=max(0.0, float(wh)),  # type: ignore[arg-type]
                    swell_period_s=max(0.0, float(wp)),  # type: ignore[arg-type]
                    swell_direction_deg=cls._normalise_direction(float(wd)),  # type: ignore[arg-type]
                    wind_speed_kmh=max(0.0, float(wind_spd)),  # type: ignore[arg-type]
                    wind_direction_deg=cls._normalise_direction(float(wind_dir)),  # type: ignore[arg-type]
                    temperature_c=float(temp) if temp is not None else None,
                    weather_code=int(wcode) if wcode is not None else None,
                )
            )

        return steps

    @staticmethod
    def _hourly(payload: dict[str, object]) -> dict[str, object]:
        """Extract the ``hourly`` block, raising ``KeyError`` if absent."""

        hourly = payload["hourly"]
        if not isinstance(hourly, dict):
            raise TypeError("'hourly' block is not an object")
        return hourly

    @staticmethod
    def _to_utc(value: datetime) -> datetime:
        """Normalise a datetime to UTC, treating naive values as UTC."""

        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        """Parse an Open-Meteo ISO timestamp and attach UTC if naive."""

        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _str_list(value: object) -> list[str]:
        """Coerce a JSON array into a ``list[str]``."""

        if not isinstance(value, list):
            raise TypeError("expected a list of timestamps")
        return [str(item) for item in value]

    @staticmethod
    def _float_list(value: object) -> list[float | None]:
        """Coerce a JSON array into a ``list[float | None]`` (nulls preserved)."""

        if not isinstance(value, list):
            raise TypeError("expected a list of numbers")
        out: list[float | None] = []
        for item in value:
            if item is None:
                out.append(None)
            else:
                out.append(float(item))
        return out
