"""``GeminiProvider`` — the default Google Gemini :class:`AIProvider`.

Generates a region :term:`Default_Preset` by prompting Google Gemini for a
structured JSON description of the six surf-preset parameters and parsing the
reply into a :class:`~brizocast.core.domain.conditions.PresetParams` — the exact
same shape as a bundled static preset, so AI and static presets are
interchangeable everywhere (Req 19.2, 19.4).

Resilience and isolation
------------------------
* Any failure talking to Gemini, or parsing its reply, is wrapped in a
  :class:`~brizocast.core.errors.ProviderRequestError` tagged with this
  provider's key. The :class:`PresetService` (task 9.2) catches it and falls
  back to bundled static presets, so a scheduler run never aborts (Req 19.8).
* The ``google-generativeai`` SDK is imported **lazily inside** the request
  path rather than at module load, so importing this module never hard-fails
  when the SDK or an API key is absent. Parsed values are clamped into valid
  ranges (and ``min_wave_m <= max_wave_m`` enforced) so a well-formed but
  out-of-range reply still yields a valid preset.

Registration
------------
On import this module registers its builder with the AI provider factory under
:data:`~brizocast.providers.ai.factory.DEFAULT_AI_PROVIDER_KEY` (``"gemini"``).
To avoid a module-load import cycle (the factory must not import this module at
load time), :func:`~brizocast.providers.ai.factory.build_ai_provider` imports
this module *lazily* the first time it resolves the ``"gemini"`` key, at which
point this module self-registers. See the factory module docstring for details.

Requirements covered: 19.1, 19.2, 19.3, 19.4.
"""

from __future__ import annotations

import importlib
import json
from typing import Any, Final

from brizocast.core.domain.conditions import (
    DIRECTION_MAX,
    DIRECTION_MIN,
    PresetParams,
)
from brizocast.core.errors import ProviderRequestError
from brizocast.core.logging import get_logger
from brizocast.providers.ai.factory import (
    DEFAULT_AI_PROVIDER_KEY,
    register_ai_provider,
)

__all__ = ["GeminiProvider"]

# The six preset fields Gemini is asked to return as a flat JSON object.
_REQUIRED_NUMERIC_FIELDS: Final[tuple[str, ...]] = (
    "min_wave_m",
    "max_wave_m",
    "min_period_s",
    "max_wind_kmh",
)
_OPTIONAL_DIRECTION_FIELDS: Final[tuple[str, ...]] = (
    "preferred_wind_dir_deg",
    "preferred_swell_dir_deg",
)


class GeminiProvider:
    """Generates region presets via Google Gemini (the default AI backend).

    Constructed from an API key and a model identifier (e.g.
    ``"gemini-1.5-flash"``). :meth:`is_available` reports whether an API key is
    configured; :meth:`generate_region_preset` prompts Gemini and maps the JSON
    reply onto a :class:`PresetParams`.
    """

    #: Stable provider key (matches :data:`DEFAULT_AI_PROVIDER_KEY`). Declared as
    #: a plain settable attribute to satisfy the ``AIProvider`` port's writable
    #: ``key`` member.
    key: str = DEFAULT_AI_PROVIDER_KEY

    def __init__(self, api_key: str, model: str) -> None:
        """Initialise the provider.

        Args:
            api_key: The Gemini API key. When empty, :meth:`is_available`
                returns ``False`` and callers fall back to static presets.
            model: The Gemini model identifier to query (e.g.
                ``"gemini-1.5-flash"``).
        """
        self._api_key = api_key
        self._model = model
        self._log = get_logger(__name__, provider=self.key)

    def is_available(self) -> bool:
        """Return whether an API key is configured (Req 15.8, 19.7)."""
        return bool(self._api_key)

    async def generate_region_preset(
        self, region: str, activity_key: str
    ) -> PresetParams:
        """Generate a region :term:`Default_Preset` via Gemini (Req 19.2, 19.4).

        Prompts Gemini for a JSON object describing the six preset parameters
        and parses it into a :class:`PresetParams`. Any API or parse failure is
        surfaced as a :class:`ProviderRequestError` tagged ``provider="gemini"``
        so the caller can fall back to bundled static presets (Req 19.8).

        Args:
            region: The region the preset is for (e.g. ``"Basque Country"``).
            activity_key: The activity key (e.g. ``"surf"``).

        Returns:
            A :class:`PresetParams` in the shared static-preset shape.

        Raises:
            ProviderRequestError: If the Gemini request fails or its reply
                cannot be parsed into a valid preset.
        """
        prompt = self._build_prompt(region, activity_key)
        try:
            raw = await self._generate_text(prompt)
        except ProviderRequestError:
            raise
        except Exception as exc:  # noqa: BLE001 - any SDK failure must degrade.
            self._log.warning(
                "Gemini request failed for region=%r activity=%r: %s",
                region,
                activity_key,
                exc,
            )
            raise ProviderRequestError(
                f"Gemini request failed for region {region!r} / activity "
                f"{activity_key!r}: {exc}",
                provider=self.key,
            ) from exc

        return self._parse_preset(raw, region=region, activity_key=activity_key)

    # -- prompt / SDK seam --------------------------------------------------- #

    @staticmethod
    def _build_prompt(region: str, activity_key: str) -> str:
        """Build the structured-JSON prompt for ``region`` / ``activity_key``."""
        return (
            f"You are a professional surf forecaster calibrating alert thresholds "
            f"for {region!r} (activity: {activity_key!r}).\n\n"
            "## Context: What these parameters mean\n"
            "These parameters define MINIMUM CONDITIONS for a WORTH-IT surf session.\n"
            "An alert fires ONLY when forecast meets or exceeds ALL of these.\n"
            "Think: 'Would a local intermediate surfer drive 30 min for this?'\n\n"
            "- min_wave_m: MINIMUM wave height worth surfing. NOT the average wave "
            "in the region — it's the minimum to justify going out. "
            "At 0.7m waves are barely rideable even on a longboard. "
            "Most surfers need at least 0.8-1.0m for a real session.\n"
            "- max_wave_m: Upper comfortable limit before it gets dangerous/messy.\n"
            "- min_period_s: MINIMUM swell period. This is crucial: period < 6s = "
            "wind chop (not real surf), 6-8s = marginal wind swell, 8-12s = "
            "quality groundswell, 12s+ = powerful long-period swell. "
            "Set this to the minimum period that produces SURFABLE waves in the region.\n"
            "- max_wind_kmh: Maximum wind before surface becomes too choppy. "
            "Light wind (0-15 km/h) = clean. Moderate (15-25) = OK if offshore. "
            "Strong (25+) = usually too messy.\n"
            "- preferred_wind_dir_deg: The OFFSHORE direction for the coast. "
            "This is the direction wind blows FROM that cleans up the waves.\n"
            "- preferred_swell_dir_deg: The dominant swell approach direction.\n\n"
            "## Scoring system\n"
            "Score = weighted sum of 5 normalized factors (0-100):\n"
            "  wave_height  30% — 1.0 in [min_wave, max_wave], ramps from 0\n"
            "  swell_period 25% — increases with period; full at min_period + 6s\n"
            "  wind_speed   20% — 1.0 at calm, 0 at 1.5×max_wind\n"
            "  wind_dir     15% — 1.0 aligned with preferred, 0 at 180° off\n"
            "  swell_dir    10% — same as wind_dir\n\n"
            "Categories: IGNORE 0-49 | RIDEABLE 50-69 | GOOD 70-84 | EXCELLENT 85+\n\n"
            "## min_alert_score\n"
            "The minimum score to send an alert. The goal: alert ONLY when "
            "conditions are BETTER THAN USUAL for the region.\n"
            "Think: 'How many days per year are genuinely worth an alert?'\n"
            "- If good surf happens ~20-30 days/year → score ~55-65 (Baltic, UK)\n"
            "- If good surf happens ~100 days/year → score ~70-75 (Atlantic France, Portugal)\n"
            "- If good surf happens ~200+ days/year → score ~80-85 (Bali, Hawaii, "
            "Philippines) — only alert on EXCEPTIONAL days\n"
            "A higher min_alert_score means fewer but higher-quality alerts.\n\n"
            "## Important\n"
            "- Be REALISTIC. A 0.7m wave with 4s period is NOT surfable. "
            "That's wind chop.\n"
            "- For cold water/rare surf: min_wave should still be 0.8m+ and "
            "min_period 6s+ otherwise the score will reward garbage conditions.\n"
            "- ELEVATE thresholds slightly — we want alerts only when genuinely "
            "worth going.\n\n"
            "Respond with ONLY a single JSON object (no markdown):\n"
            "  min_wave_m, max_wave_m, min_period_s, max_wind_kmh,\n"
            "  preferred_wind_dir_deg (0-360 or null),\n"
            "  preferred_swell_dir_deg (0-360 or null),\n"
            "  min_alert_score (0-100)\n"
        )

    async def _generate_text(self, prompt: str) -> str:
        """Send ``prompt`` to Gemini and return the raw text reply.

        Uses the ``google.genai`` SDK (the new, maintained replacement for
        the deprecated ``google.generativeai`` package).

        Raises:
            ProviderRequestError: If the SDK is unavailable or the reply is
                empty.
        """
        try:
            from google import genai as google_genai
        except Exception as exc:  # noqa: BLE001 - missing SDK -> degrade.
            raise ProviderRequestError(
                "The google-genai SDK is not available.",
                provider=self.key,
            ) from exc

        client = google_genai.Client(api_key=self._api_key)
        response = await client.aio.models.generate_content(
            model=self._model,
            contents=prompt,
        )

        text = response.text if response.text else None
        if not text:
            raise ProviderRequestError(
                "Gemini returned an empty response.",
                provider=self.key,
            )
        return str(text)

    # -- parsing / validation ------------------------------------------------ #

    def _parse_preset(
        self, raw: str, *, region: str, activity_key: str
    ) -> PresetParams:
        """Parse and validate Gemini's reply into a :class:`PresetParams`.

        The numeric fields are clamped to be non-negative and the wave band is
        normalised so ``min_wave_m <= max_wave_m``; direction fields are clamped
        into ``[0, 360]`` or treated as unset. Any structural failure raises a
        :class:`ProviderRequestError`.
        """
        try:
            payload = json.loads(_extract_json_object(raw))
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            data: dict[str, Any] = payload

            min_wave = max(0.0, _as_float(data["min_wave_m"]))
            max_wave = max(0.0, _as_float(data["max_wave_m"]))
            # Enforce the domain invariant min_wave <= max_wave by clamping the
            # minimum down to the maximum when the model returns them reversed.
            min_wave = min(min_wave, max_wave)

            return PresetParams(
                min_wave_m=min_wave,
                max_wave_m=max_wave,
                min_period_s=max(0.0, _as_float(data["min_period_s"])),
                max_wind_kmh=max(0.0, _as_float(data["max_wind_kmh"])),
                preferred_wind_dir_deg=_clamp_direction(
                    data.get("preferred_wind_dir_deg")
                ),
                preferred_swell_dir_deg=_clamp_direction(
                    data.get("preferred_swell_dir_deg")
                ),
                min_alert_score=_parse_alert_score(data.get("min_alert_score")),
            )
        except Exception as exc:  # noqa: BLE001 - any parse failure -> degrade.
            self._log.warning(
                "Failed to parse Gemini preset for region=%r activity=%r: %s",
                region,
                activity_key,
                exc,
            )
            raise ProviderRequestError(
                f"Could not parse Gemini preset for region {region!r} / "
                f"activity {activity_key!r}: {exc}",
                provider=self.key,
            ) from exc


def _as_float(value: Any) -> float:
    """Coerce a JSON scalar to ``float`` (rejecting booleans and non-numbers)."""
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly.
        raise TypeError("boolean is not a valid numeric value")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"cannot interpret {value!r} as a number")


def _parse_alert_score(value: Any) -> int | None:
    """Parse min_alert_score from Gemini response, clamped to 0-100."""
    if value is None:
        return None
    try:
        score = int(_as_float(value))
        return max(0, min(100, score))
    except (TypeError, ValueError):
        return None


def _clamp_direction(value: Any) -> float | None:
    """Clamp a direction into ``[0, 360]`` degrees, or return ``None`` if unset."""
    if value is None:
        return None
    degrees = _as_float(value)
    if degrees < DIRECTION_MIN:
        return DIRECTION_MIN
    if degrees > DIRECTION_MAX:
        return DIRECTION_MAX
    return degrees


def _extract_json_object(raw: str) -> str:
    """Extract the first ``{...}`` JSON object from ``raw`` (tolerating fences).

    Gemini frequently wraps JSON in markdown code fences or adds surrounding
    prose; this returns the substring spanning the first ``{`` to the last
    ``}`` so :func:`json.loads` can parse it.
    """
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in response")
    return raw[start : end + 1]


# Register the Gemini builder under the default AI provider key. This runs when
# the module is imported, which the factory does lazily the first time it
# resolves the "gemini" key (avoiding a module-load import cycle).
register_ai_provider(
    DEFAULT_AI_PROVIDER_KEY,
    lambda api_key, model: GeminiProvider(api_key, model),
)
