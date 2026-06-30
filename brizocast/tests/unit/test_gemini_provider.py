"""Unit tests for the Gemini :class:`AIProvider` implementation (task 9.1).

The ``google-generativeai`` SDK is replaced with an in-process fake (installed
into ``sys.modules``) so no real network calls are made. Tests cover:

* ``is_available`` reflecting API-key presence,
* mapping a JSON reply onto a :class:`PresetParams` (including fence tolerance),
* clamping/normalising out-of-range and reversed values,
* API failures and unparseable replies surfacing as :class:`ProviderRequestError`,
* self-registration with the AI provider factory.

Requirements covered: 19.1, 19.2, 19.3, 19.4.
"""

from __future__ import annotations

import sys
from typing import Any, cast

import pytest

from brizocast.core.domain.conditions import PresetParams
from brizocast.core.errors import ProviderRequestError
from brizocast.core.ports.ai_provider import AIProvider
from brizocast.providers.ai.factory import (
    DEFAULT_AI_PROVIDER_KEY,
    registered_ai_provider_keys,
)
from brizocast.providers.ai.gemini import GeminiProvider

_SDK_MODULE = "google.generativeai"


# --- SDK fake --------------------------------------------------------------- #


class _FakeResponse:
    """Mimics the Gemini SDK response object (only the ``text`` attribute)."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModel:
    """Mimics ``genai.GenerativeModel`` with a canned reply or error."""

    def __init__(self, reply: str, error: Exception | None) -> None:
        self._reply = reply
        self._error = error

    async def generate_content_async(self, prompt: str) -> _FakeResponse:
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._reply)


class _FakeGenAI:
    """Stand-in for the ``google.generativeai`` module."""

    def __init__(self, reply: str = "", error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error
        self.configured_key: str | None = None

    def configure(self, *, api_key: str) -> None:
        self.configured_key = api_key

    def GenerativeModel(self, model_name: str) -> _FakeModel:  # noqa: N802
        return _FakeModel(self._reply, self._error)


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply: str = "",
    error: Exception | None = None,
) -> _FakeGenAI:
    """Install a fake ``google.generativeai`` module and return it."""
    fake = _FakeGenAI(reply=reply, error=error)
    monkeypatch.setitem(sys.modules, _SDK_MODULE, cast(Any, fake))
    return fake


# --- is_available ----------------------------------------------------------- #


def test_is_available_true_when_key_present() -> None:
    provider = GeminiProvider("secret-key", "gemini-1.5-flash")
    assert provider.is_available() is True
    assert provider.key == DEFAULT_AI_PROVIDER_KEY
    assert isinstance(provider, AIProvider)


def test_is_available_false_when_key_blank() -> None:
    assert GeminiProvider("", "gemini-1.5-flash").is_available() is False


# --- generate_region_preset: success mapping -------------------------------- #


async def test_generate_region_preset_maps_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = (
        "```json\n"
        '{"min_wave_m": 1.0, "max_wave_m": 2.5, "min_period_s": 10, '
        '"max_wind_kmh": 25, "preferred_wind_dir_deg": 90, '
        '"preferred_swell_dir_deg": 270}\n'
        "```"
    )
    fake = _install_fake_sdk(monkeypatch, reply=reply)

    provider = GeminiProvider("secret-key", "gemini-1.5-flash")
    preset = await provider.generate_region_preset("Basque Country", "surf")

    assert preset == PresetParams(
        min_wave_m=1.0,
        max_wave_m=2.5,
        min_period_s=10.0,
        max_wind_kmh=25.0,
        preferred_wind_dir_deg=90.0,
        preferred_swell_dir_deg=270.0,
    )
    # The API key was passed through to the SDK.
    assert fake.configured_key == "secret-key"


async def test_generate_region_preset_clamps_and_orders_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reversed wave band, negative period/wind, out-of-range direction, null dir.
    reply = (
        '{"min_wave_m": 5, "max_wave_m": 2, "min_period_s": -3, '
        '"max_wind_kmh": -1, "preferred_wind_dir_deg": 400, '
        '"preferred_swell_dir_deg": null}'
    )
    _install_fake_sdk(monkeypatch, reply=reply)

    provider = GeminiProvider("secret-key", "gemini-1.5-flash")
    preset = await provider.generate_region_preset("Peniche", "surf")

    assert preset.min_wave_m == 2.0  # clamped down to max
    assert preset.max_wave_m == 2.0
    assert preset.min_period_s == 0.0
    assert preset.max_wind_kmh == 0.0
    assert preset.preferred_wind_dir_deg == 360.0  # clamped to max bearing
    assert preset.preferred_swell_dir_deg is None


# --- generate_region_preset: failure paths ---------------------------------- #


async def test_api_error_raises_provider_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch, error=RuntimeError("network down"))

    provider = GeminiProvider("secret-key", "gemini-1.5-flash")
    with pytest.raises(ProviderRequestError) as exc:
        await provider.generate_region_preset("Hossegor", "surf")
    assert exc.value.provider == DEFAULT_AI_PROVIDER_KEY


async def test_unparseable_reply_raises_provider_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch, reply="sorry, I cannot help with that")

    provider = GeminiProvider("secret-key", "gemini-1.5-flash")
    with pytest.raises(ProviderRequestError) as exc:
        await provider.generate_region_preset("Landes", "surf")
    assert exc.value.provider == DEFAULT_AI_PROVIDER_KEY


async def test_empty_reply_raises_provider_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch, reply="")

    provider = GeminiProvider("secret-key", "gemini-1.5-flash")
    with pytest.raises(ProviderRequestError) as exc:
        await provider.generate_region_preset("Donegal", "surf")
    assert exc.value.provider == DEFAULT_AI_PROVIDER_KEY


async def test_missing_sdk_raises_provider_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Setting the module to None makes ``import google.generativeai`` raise.
    monkeypatch.setitem(sys.modules, _SDK_MODULE, cast(Any, None))

    provider = GeminiProvider("secret-key", "gemini-1.5-flash")
    with pytest.raises(ProviderRequestError) as exc:
        await provider.generate_region_preset("Ericeira", "surf")
    assert exc.value.provider == DEFAULT_AI_PROVIDER_KEY


# --- registration ----------------------------------------------------------- #


def test_provider_registers_with_factory() -> None:
    # Importing this module imports gemini, which self-registers its builder.
    assert DEFAULT_AI_PROVIDER_KEY in registered_ai_provider_keys()
