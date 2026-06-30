"""Tests for the Open-Meteo Marine forecast provider and the provider factory.

These tests exercise the response → ``Forecast`` mapping against a recorded
sample payload and the factory's key resolution, all without real network
access: a fake ``httpx.AsyncClient`` is injected so no HTTP call is made.

Covers task 3.2 (Requirements 6.2, 6.3, 6.4, 6.5, 15.5, 18.2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from brizocast.config.settings import Settings
from brizocast.core.domain.forecast import ForecastWindow
from brizocast.core.errors import ProviderRequestError
from brizocast.providers.forecast.factory import (
    DEFAULT_FORECAST_PROVIDER_KEY,
    build_forecast_provider,
)
from brizocast.providers.forecast.open_meteo_marine import (
    _MARINE_URL,
    _WEATHER_URL,
    OpenMeteoMarineProvider,
)
from brizocast.providers.forecast.stormglass import StormglassProvider
from brizocast.providers.forecast.windy import WindyProvider

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Recorded sample payloads (trimmed to the requested hourly variables).
# --------------------------------------------------------------------------- #
_MARINE_SAMPLE: dict[str, Any] = {
    "hourly": {
        "time": [
            "2025-06-01T00:00",
            "2025-06-01T01:00",
            "2025-06-01T02:00",
        ],
        "wave_height": [1.2, 1.5, 1.8],
        "wave_direction": [210.0, 215.0, 400.0],  # 400 -> normalised to 40
        "wave_period": [8.0, 8.5, 9.0],
    }
}

_WEATHER_SAMPLE: dict[str, Any] = {
    "hourly": {
        "time": [
            "2025-06-01T00:00",
            "2025-06-01T01:00",
            "2025-06-01T02:00",
        ],
        "wind_speed_10m": [12.0, 14.0, 16.0],
        "wind_direction_10m": [180.0, 190.0, 200.0],
    }
}


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "error", request=request, response=response
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    """Routes GETs to the marine/weather sample payloads by URL."""

    def __init__(
        self,
        marine: dict[str, Any],
        weather: dict[str, Any],
        *,
        raise_exc: Exception | None = None,
    ) -> None:
        self._marine = marine
        self._weather = weather
        self._raise_exc = raise_exc
        self.calls: list[str] = []

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        self.calls.append(url)
        if self._raise_exc is not None:
            raise self._raise_exc
        if url == _MARINE_URL:
            return _FakeResponse(self._marine)
        if url == _WEATHER_URL:
            return _FakeResponse(self._weather)
        raise AssertionError(f"unexpected URL {url!r}")


def _window() -> ForecastWindow:
    return ForecastWindow(
        start=datetime(2025, 6, 1, 0, 0, tzinfo=UTC),
        end=datetime(2025, 6, 1, 12, 0, tzinfo=UTC),
    )


async def test_maps_sample_payload_to_forecast() -> None:
    """The two hourly responses align by timestamp into complete steps."""

    client = _FakeAsyncClient(_MARINE_SAMPLE, _WEATHER_SAMPLE)
    provider = OpenMeteoMarineProvider(client=client)  # type: ignore[arg-type]

    forecast = await provider.get_forecast(43.4, -2.7, _window())

    assert forecast.spot_id == "43.4000,-2.7000"
    assert len(forecast.steps) == 3

    first = forecast.steps[0]
    assert first.timestamp == datetime(2025, 6, 1, 0, 0, tzinfo=UTC)
    assert first.wave_height_m == 1.2
    assert first.swell_period_s == 8.0
    assert first.swell_direction_deg == 210.0
    assert first.wind_speed_kmh == 12.0
    assert first.wind_direction_deg == 180.0

    # 400 degrees in the sample is folded into [0, 360).
    assert forecast.steps[2].swell_direction_deg == 40.0

    # Every step carries all six fields (Req 6.4) within bounds.
    for step in forecast.steps:
        assert step.wave_height_m >= 0.0
        assert step.swell_period_s >= 0.0
        assert 0.0 <= step.swell_direction_deg <= 360.0
        assert step.wind_speed_kmh >= 0.0
        assert 0.0 <= step.wind_direction_deg <= 360.0


async def test_filters_steps_outside_window() -> None:
    """Steps whose timestamp falls outside the window are dropped."""

    client = _FakeAsyncClient(_MARINE_SAMPLE, _WEATHER_SAMPLE)
    provider = OpenMeteoMarineProvider(client=client)  # type: ignore[arg-type]

    narrow = ForecastWindow(
        start=datetime(2025, 6, 1, 1, 0, tzinfo=UTC),
        end=datetime(2025, 6, 1, 1, 0, tzinfo=UTC),
    )
    forecast = await provider.get_forecast(43.4, -2.7, narrow)

    assert len(forecast.steps) == 1
    assert forecast.steps[0].timestamp == datetime(2025, 6, 1, 1, 0, tzinfo=UTC)


async def test_unaligned_timestamps_are_skipped() -> None:
    """A marine timestamp with no matching wind sample produces no step."""

    weather = {
        "hourly": {
            "time": ["2025-06-01T00:00", "2025-06-01T02:00"],
            "wind_speed_10m": [12.0, 16.0],
            "wind_direction_10m": [180.0, 200.0],
        }
    }
    client = _FakeAsyncClient(_MARINE_SAMPLE, weather)
    provider = OpenMeteoMarineProvider(client=client)  # type: ignore[arg-type]

    forecast = await provider.get_forecast(43.4, -2.7, _window())

    times = [s.timestamp.hour for s in forecast.steps]
    assert times == [0, 2]  # 01:00 had no wind sample -> skipped


async def test_network_failure_raises_provider_error() -> None:
    """A raw httpx error is wrapped as ProviderRequestError (Req 6.5, 18.2)."""

    client = _FakeAsyncClient(
        _MARINE_SAMPLE,
        _WEATHER_SAMPLE,
        raise_exc=httpx.ConnectError("boom"),
    )
    provider = OpenMeteoMarineProvider(client=client)  # type: ignore[arg-type]

    with pytest.raises(ProviderRequestError) as excinfo:
        await provider.get_forecast(43.4, -2.7, _window())
    assert excinfo.value.provider == "open_meteo_marine"


async def test_malformed_payload_raises_provider_error() -> None:
    """A response missing the hourly block is wrapped as ProviderRequestError."""

    client = _FakeAsyncClient({"unexpected": True}, _WEATHER_SAMPLE)
    provider = OpenMeteoMarineProvider(client=client)  # type: ignore[arg-type]

    with pytest.raises(ProviderRequestError) as excinfo:
        await provider.get_forecast(43.4, -2.7, _window())
    assert excinfo.value.provider == "open_meteo_marine"


# --------------------------------------------------------------------------- #
# Factory resolution (Req 6.3, 15.5).
# --------------------------------------------------------------------------- #
def _settings(provider_key: str) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="x",
        FORECAST_PROVIDER=provider_key,
    )


def test_factory_resolves_open_meteo_marine() -> None:
    provider = build_forecast_provider(_settings("open_meteo_marine"))
    assert isinstance(provider, OpenMeteoMarineProvider)
    assert provider.key == "open_meteo_marine"


def test_factory_resolves_stormglass_and_windy() -> None:
    assert isinstance(build_forecast_provider(_settings("stormglass")), StormglassProvider)
    assert isinstance(build_forecast_provider(_settings("windy")), WindyProvider)


def test_factory_defaults_on_unknown_key() -> None:
    provider = build_forecast_provider(_settings("does-not-exist"))
    assert isinstance(provider, OpenMeteoMarineProvider)
    assert provider.key == DEFAULT_FORECAST_PROVIDER_KEY


def test_factory_defaults_on_empty_key() -> None:
    provider = build_forecast_provider(_settings(""))
    assert isinstance(provider, OpenMeteoMarineProvider)


def test_factory_injects_client() -> None:
    client = _FakeAsyncClient(_MARINE_SAMPLE, _WEATHER_SAMPLE)
    provider = build_forecast_provider(_settings(""), client=client)  # type: ignore[arg-type]
    assert isinstance(provider, OpenMeteoMarineProvider)
