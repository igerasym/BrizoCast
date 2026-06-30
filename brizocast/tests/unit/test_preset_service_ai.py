"""Unit tests for AI/static preset resolution in :class:`PresetService` (Req 19).

Covers :meth:`PresetService.get_region_presets`, the seam between the optional
:class:`~brizocast.core.ports.ai_provider.AIProvider` and the bundled static
``Default_Presets``:

* an **available** provider has its generated preset prepended to the static
  defaults, interchangeable in shape (Req 19.5);
* an **unavailable** provider (disabled / unkeyed / not injected) yields the
  static defaults only (Req 15.6, 15.8, 19.6, 19.7);
* a provider that raises :class:`ProviderRequestError` — or any other error —
  is logged and falls back silently to the static defaults so a scheduler run
  continues (Req 19.8, 19.9);
* an AI-generated preset scores **identically** to an equivalent static preset
  carrying the same parameters, demonstrating interchangeability through the
  scorer (Req 19.5; supports Property 23).

These are pure unit tests: ``get_region_presets`` performs no database I/O, so
the service is constructed with a throwaway in-memory session factory that is
never exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brizocast.activities.surf.conditions import SurfConditions
from brizocast.activities.surf.presets import first_default_for_region
from brizocast.activities.surf.scorer import SurfScorer
from brizocast.core.domain.conditions import PresetParams
from brizocast.core.domain.daylight import DaylightInfo
from brizocast.core.domain.forecast import ForecastStep
from brizocast.core.errors import ProviderRequestError
from brizocast.database.session import create_engine, create_session_factory
from brizocast.services.preset_service import (
    PresetOption,
    PresetService,
    PresetSource,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fake AIProvider implementations (satisfy the AIProvider port structurally).
# --------------------------------------------------------------------------- #
class _FakeAIProvider:
    """An available provider returning a fixed :class:`PresetParams`."""

    key = "fake-ai"

    def __init__(self, params: PresetParams) -> None:
        self._params = params
        self.calls: list[tuple[str, str]] = []

    def is_available(self) -> bool:
        return True

    async def generate_region_preset(
        self, region: str, activity_key: str
    ) -> PresetParams:
        self.calls.append((region, activity_key))
        return self._params


class _UnavailableAIProvider:
    """A provider that is never available (like ``NullAIProvider``)."""

    key = "unavailable"

    def is_available(self) -> bool:
        return False

    async def generate_region_preset(
        self, region: str, activity_key: str
    ) -> PresetParams:  # pragma: no cover - must never be called.
        raise AssertionError("generate_region_preset must not be called")


class _FailingAIProvider:
    """An available provider whose generation raises a given exception."""

    key = "failing-ai"

    def __init__(self, error: Exception) -> None:
        self._error = error

    def is_available(self) -> bool:
        return True

    async def generate_region_preset(
        self, region: str, activity_key: str
    ) -> PresetParams:
        raise self._error


def _make_service(ai_provider: object | None) -> PresetService:
    """Build a PresetService with a throwaway in-memory session factory."""
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    factory = create_session_factory(engine)
    # ``get_region_presets`` never touches the DB, so the engine is never used.
    return PresetService(factory, ai_provider=ai_provider)  # type: ignore[arg-type]


_AI_PARAMS = PresetParams(
    min_wave_m=1.1,
    max_wave_m=2.8,
    min_period_s=10.5,
    max_wind_kmh=23.0,
    preferred_wind_dir_deg=80.0,
    preferred_swell_dir_deg=295.0,
)


async def test_available_provider_prepends_ai_preset() -> None:
    """An available provider's preset is prepended to the static defaults (Req 19.5)."""
    provider = _FakeAIProvider(_AI_PARAMS)
    service = _make_service(provider)

    options = await service.get_region_presets("Peniche", "surf")

    # The provider was asked for exactly the requested region/activity.
    assert provider.calls == [("Peniche", "surf")]

    # First option is the AI-generated default; the rest are the static ones.
    assert options[0].source is PresetSource.AI_GENERATED
    assert options[0].ai_generated is True
    assert options[0].preset_id is None  # ephemeral, not persisted.
    assert options[0].params == _AI_PARAMS
    assert options[0].is_default is True

    static_options = [o for o in options if o.source is PresetSource.STATIC_DEFAULT]
    assert len(options) == len(static_options) + 1
    assert all(not o.ai_generated for o in static_options)


async def test_unavailable_provider_returns_static_only() -> None:
    """An unavailable provider yields the static defaults only (Req 15.8, 19.6/7)."""
    service = _make_service(_UnavailableAIProvider())

    options = await service.get_region_presets("Peniche", "surf")

    assert options
    assert all(o.source is PresetSource.STATIC_DEFAULT for o in options)
    assert all(not o.ai_generated for o in options)


async def test_no_provider_injected_returns_static_only() -> None:
    """With no provider injected, only static defaults are returned (Req 15.6)."""
    service = _make_service(None)

    options = await service.get_region_presets("Hossegor")

    assert options
    assert all(o.source is PresetSource.STATIC_DEFAULT for o in options)


async def test_none_region_skips_ai_and_returns_empty() -> None:
    """A None region returns an empty list without calling AI (no region-specific statics)."""
    provider = _FakeAIProvider(_AI_PARAMS)
    service = _make_service(provider)

    options = await service.get_region_presets(None)

    assert provider.calls == []  # no single region to generate for.
    assert options == []


async def test_provider_request_error_falls_back_to_static() -> None:
    """A ProviderRequestError falls back silently to the static defaults (Req 19.8/9)."""
    provider = _FailingAIProvider(
        ProviderRequestError("boom", provider="failing-ai")
    )
    service = _make_service(provider)

    options = await service.get_region_presets("Donegal", "surf")

    static = first_default_for_region("Donegal")
    assert options
    assert all(o.source is PresetSource.STATIC_DEFAULT for o in options)
    assert options[0].name == static.name


async def test_unexpected_provider_error_falls_back_to_static() -> None:
    """Any unexpected provider error also degrades to the static defaults (Req 19.8)."""
    provider = _FailingAIProvider(RuntimeError("kaboom"))
    service = _make_service(provider)

    options = await service.get_region_presets("Landes", "surf")

    assert options
    assert all(o.source is PresetSource.STATIC_DEFAULT for o in options)


def _conditions_from_params(params: PresetParams) -> SurfConditions:
    """Project a :class:`PresetParams` onto :class:`SurfConditions` for scoring."""
    return SurfConditions(
        min_wave_m=params.min_wave_m,
        max_wave_m=params.max_wave_m,
        min_period_s=params.min_period_s,
        max_wind_kmh=params.max_wind_kmh,
        preferred_wind_dir_deg=params.preferred_wind_dir_deg,
        preferred_swell_dir_deg=params.preferred_swell_dir_deg,
    )


async def test_ai_preset_scores_identically_to_equivalent_static_preset() -> None:
    """An AI preset scores the same as a static preset with identical params (Property 23).

    Build a static default's parameters, feed the *same* parameters back through
    a fake AIProvider, and confirm the scorer produces an identical result for
    the AI-generated option and the equivalent static-shaped preset.
    """
    static_params = first_default_for_region("Ericeira").params
    provider = _FakeAIProvider(static_params)
    service = _make_service(provider)

    options = await service.get_region_presets("Ericeira", "surf")
    ai_option = options[0]
    assert ai_option.source is PresetSource.AI_GENERATED

    scorer = SurfScorer()
    step = ForecastStep(
        timestamp=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
        wave_height_m=1.6,
        swell_period_s=12.0,
        swell_direction_deg=300.0,
        wind_speed_kmh=12.0,
        wind_direction_deg=45.0,
    )
    daylight = DaylightInfo(
        sunrise=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
        sunset=datetime(2025, 6, 1, 21, 0, tzinfo=UTC),
    )

    ai_result = scorer.score(step, _conditions_from_params(ai_option.params), daylight)
    static_result = scorer.score(
        step, _conditions_from_params(static_params), daylight
    )

    assert ai_result.score == static_result.score
    assert ai_result.category == static_result.category
    assert ai_result.breakdown == static_result.breakdown


def test_preset_option_ai_generated_is_default() -> None:
    """An AI-generated option is treated as a default, not a user custom."""
    option = PresetOption(
        name="X — AI Suggested",
        region="X",
        params=_AI_PARAMS,
        source=PresetSource.AI_GENERATED,
        preset_id=None,
        ai_generated=True,
    )
    assert option.is_default is True
