"""Unit tests for the AI provider factory, NullAIProvider, and container wiring.

Covers the AI-provider resolution logic and the lazy registration of the three
external-data provider factories on the DI container (Req 6.3, 15.5, 15.7,
15.10, 19.1, 19.3 — supporting Property 22).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from brizocast.config.settings import Settings
from brizocast.core.container import (
    AI_PROVIDER_KEY,
    FORECAST_PROVIDER_KEY,
    GEOCODING_PROVIDER_KEY,
    Container,
)
from brizocast.core.domain.conditions import PresetParams
from brizocast.core.errors import ProviderRequestError
from brizocast.core.ports.ai_provider import AIProvider
from brizocast.providers.ai import factory as ai_factory
from brizocast.providers.ai.factory import (
    DEFAULT_AI_PROVIDER_KEY,
    build_ai_provider,
    register_ai_provider,
    registered_ai_provider_keys,
)
from brizocast.providers.ai.null_ai import NullAIProvider


def _settings(**overrides: object) -> Settings:
    """Build a validated :class:`Settings` with a token and the given overrides."""
    base: dict[str, object] = {"TELEGRAM_BOT_TOKEN": "test-token"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Snapshot and restore the module-global AI provider registry per test."""
    snapshot = dict(ai_factory._AI_PROVIDER_REGISTRY)
    try:
        yield
    finally:
        ai_factory._AI_PROVIDER_REGISTRY.clear()
        ai_factory._AI_PROVIDER_REGISTRY.update(snapshot)


class _FakeAIProvider:
    """Minimal registered AIProvider used to exercise the registry path."""

    key = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def is_available(self) -> bool:
        return True

    async def generate_region_preset(
        self, region: str, activity_key: str
    ) -> PresetParams:
        return PresetParams(
            min_wave_m=0.5, max_wave_m=2.0, min_period_s=8.0, max_wind_kmh=20.0
        )


# --- NullAIProvider --------------------------------------------------------- #


def test_null_provider_is_never_available() -> None:
    provider = NullAIProvider()
    assert provider.key == "null"
    assert provider.is_available() is False
    assert isinstance(provider, AIProvider)


async def test_null_provider_generate_raises() -> None:
    provider = NullAIProvider()
    with pytest.raises(ProviderRequestError) as exc:
        await provider.generate_region_preset("Basque Country", "surf")
    assert exc.value.provider == "null"


# --- build_ai_provider resolution ------------------------------------------- #


def test_disabled_resolves_to_null() -> None:
    provider = build_ai_provider(_settings(AI_ENABLED=False))
    assert isinstance(provider, NullAIProvider)
    assert provider.is_available() is False


def test_enabled_but_unkeyed_resolves_to_null() -> None:
    provider = build_ai_provider(_settings(AI_ENABLED=True, AI_API_KEY=None))
    assert isinstance(provider, NullAIProvider)


def test_enabled_keyed_gemini_resolves_to_gemini_provider() -> None:
    # Importing the gemini module self-registers its builder (task 9.1); an
    # enabled, keyed "gemini" configuration then resolves to a GeminiProvider.
    from brizocast.providers.ai.gemini import GeminiProvider

    assert DEFAULT_AI_PROVIDER_KEY in registered_ai_provider_keys()
    provider = build_ai_provider(
        _settings(AI_ENABLED=True, AI_API_KEY="secret", AI_PROVIDER="gemini")
    )
    assert isinstance(provider, GeminiProvider)
    assert provider.is_available() is True


def test_unspecified_provider_defaults_to_gemini_key() -> None:
    register_ai_provider(DEFAULT_AI_PROVIDER_KEY, _FakeAIProvider)
    # Empty AI_PROVIDER must resolve to the default "gemini" key (Req 15.7).
    provider = build_ai_provider(
        _settings(AI_ENABLED=True, AI_API_KEY="secret", AI_PROVIDER="")
    )
    assert isinstance(provider, _FakeAIProvider)
    assert provider.api_key == "secret"


def test_registered_builder_is_used() -> None:
    register_ai_provider("gemini", _FakeAIProvider)
    provider = build_ai_provider(
        _settings(
            AI_ENABLED=True,
            AI_API_KEY="abc",
            AI_PROVIDER="gemini",
            AI_MODEL="gemini-1.5-flash",
        )
    )
    assert isinstance(provider, _FakeAIProvider)
    assert provider.model == "gemini-1.5-flash"


def test_unknown_provider_key_falls_back_to_null() -> None:
    provider = build_ai_provider(
        _settings(AI_ENABLED=True, AI_API_KEY="abc", AI_PROVIDER="does-not-exist")
    )
    assert isinstance(provider, NullAIProvider)


# --- container wiring -------------------------------------------------------- #


def test_container_registers_three_provider_factories() -> None:
    container = Container(_settings(AI_ENABLED=False))
    assert container.is_registered(FORECAST_PROVIDER_KEY)
    assert container.is_registered(GEOCODING_PROVIDER_KEY)
    assert container.is_registered(AI_PROVIDER_KEY)


def test_container_resolves_ai_provider() -> None:
    container = Container(_settings(AI_ENABLED=False))
    provider = container.resolve(AI_PROVIDER_KEY, NullAIProvider)
    assert isinstance(provider, NullAIProvider)
    assert isinstance(provider, AIProvider)
    # Resolved as a cached singleton.
    assert container.resolve(AI_PROVIDER_KEY, NullAIProvider) is provider
