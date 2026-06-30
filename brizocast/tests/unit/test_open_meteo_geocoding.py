"""Unit tests for the Open-Meteo geocoding provider and factory (task 3.5).

All HTTP is mocked with :class:`httpx.MockTransport` so no real network call is
made. The tests cover:

* mapping a sample Open-Meteo JSON payload to :class:`GeoCandidate`s (Req 2.3,
  2.4, 2.5),
* the empty-results case returning ``[]`` so the caller can re-prompt (Req 2.6),
* failure modes (network error, non-success status, malformed payload) raising
  :class:`ProviderRequestError` tagged with the provider key (Req 2.11, 18.2),
* the factory resolving the default and unknown keys to the Open-Meteo provider.
"""

from __future__ import annotations

import httpx
import pytest

from brizocast.config.settings import Settings
from brizocast.core.domain.geo import GeoCandidate
from brizocast.core.errors import ProviderRequestError
from brizocast.providers.geocoding.factory import build_geocoding_provider
from brizocast.providers.geocoding.open_meteo_geocoding import (
    OPEN_METEO_GEOCODING_KEY,
    OpenMeteoGeocodingProvider,
)

pytestmark = pytest.mark.unit


# A representative Open-Meteo Geocoding response for "Hossegor".
_SAMPLE_PAYLOAD = {
    "results": [
        {
            "id": 3013736,
            "name": "Hossegor",
            "latitude": 43.66667,
            "longitude": -1.4,
            "country_code": "FR",
            "country": "France",
            "admin1": "Nouvelle-Aquitaine",
            "timezone": "Europe/Paris",
        },
        {
            "id": 6457199,
            "name": "Soorts-Hossegor",
            "latitude": 43.66174,
            "longitude": -1.39555,
            "country_code": "FR",
            "country": "France",
            "timezone": "Europe/Paris",
        },
    ],
    "generationtime_ms": 0.42,
}

# Open-Meteo omits the ``results`` key entirely when nothing matches.
_EMPTY_PAYLOAD = {"generationtime_ms": 0.13}


def _provider_with_handler(
    handler: object,
) -> OpenMeteoGeocodingProvider:
    """Build a provider backed by a mock transport using ``handler``."""
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    return OpenMeteoGeocodingProvider(client=client)


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #
async def test_search_maps_payload_to_candidates() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_SAMPLE_PAYLOAD)

    provider = _provider_with_handler(handler)
    candidates = await provider.search("Hossegor", limit=5)

    assert len(candidates) == 2
    first = candidates[0]
    assert isinstance(first, GeoCandidate)
    assert first.name == "Hossegor"
    assert first.lat == pytest.approx(43.66667)
    assert first.lon == pytest.approx(-1.4)
    assert first.city == "Hossegor"
    assert first.country == "France"
    # The query and limit are forwarded to the API as name/count.
    assert "name=Hossegor" in captured["url"]
    assert "count=5" in captured["url"]


async def test_search_handles_missing_country_as_none() -> None:
    payload = {
        "results": [
            {"name": "Nowhere", "latitude": 10.0, "longitude": 20.0},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider = _provider_with_handler(handler)
    candidates = await provider.search("Nowhere")

    assert len(candidates) == 1
    assert candidates[0].country is None
    assert candidates[0].city == "Nowhere"


# --------------------------------------------------------------------------- #
# Empty results (Req 2.6)
# --------------------------------------------------------------------------- #
async def test_search_returns_empty_list_when_no_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_EMPTY_PAYLOAD)

    provider = _provider_with_handler(handler)
    candidates = await provider.search("zzzznowhere")

    assert candidates == []


async def test_search_returns_empty_list_for_empty_results_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    provider = _provider_with_handler(handler)
    assert await provider.search("zzzz") == []


# --------------------------------------------------------------------------- #
# Failure modes (Req 2.11, 18.2)
# --------------------------------------------------------------------------- #
async def test_network_error_raises_provider_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _provider_with_handler(handler)
    with pytest.raises(ProviderRequestError) as exc_info:
        await provider.search("Hossegor")
    assert exc_info.value.provider == OPEN_METEO_GEOCODING_KEY


async def test_http_error_status_raises_provider_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    provider = _provider_with_handler(handler)
    with pytest.raises(ProviderRequestError) as exc_info:
        await provider.search("Hossegor")
    assert exc_info.value.provider == OPEN_METEO_GEOCODING_KEY


async def test_invalid_json_raises_provider_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    provider = _provider_with_handler(handler)
    with pytest.raises(ProviderRequestError) as exc_info:
        await provider.search("Hossegor")
    assert exc_info.value.provider == OPEN_METEO_GEOCODING_KEY


async def test_malformed_entry_raises_provider_request_error() -> None:
    payload = {"results": [{"name": "Broken", "latitude": "not-a-number"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider = _provider_with_handler(handler)
    with pytest.raises(ProviderRequestError) as exc_info:
        await provider.search("Broken")
    assert exc_info.value.provider == OPEN_METEO_GEOCODING_KEY


async def test_out_of_range_coordinates_raise_provider_request_error() -> None:
    payload = {"results": [{"name": "Bad", "latitude": 999.0, "longitude": 0.0}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider = _provider_with_handler(handler)
    with pytest.raises(ProviderRequestError):
        await provider.search("Bad")


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def _settings(provider_key: str) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        GEOCODING_PROVIDER=provider_key,
    )


def test_factory_builds_open_meteo_for_default_key() -> None:
    provider = build_geocoding_provider(_settings("open_meteo_geocoding"))
    assert isinstance(provider, OpenMeteoGeocodingProvider)
    assert provider.key == OPEN_METEO_GEOCODING_KEY


def test_factory_falls_back_to_open_meteo_for_unknown_key() -> None:
    provider = build_geocoding_provider(_settings("does_not_exist"))
    assert isinstance(provider, OpenMeteoGeocodingProvider)


def test_factory_falls_back_to_open_meteo_for_empty_key() -> None:
    provider = build_geocoding_provider(_settings(""))
    assert isinstance(provider, OpenMeteoGeocodingProvider)


def test_factory_injects_client() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_EMPTY_PAYLOAD))
    client = httpx.AsyncClient(transport=transport)
    provider = build_geocoding_provider(_settings("open_meteo_geocoding"), client=client)
    assert isinstance(provider, OpenMeteoGeocodingProvider)
    assert provider._client is client
