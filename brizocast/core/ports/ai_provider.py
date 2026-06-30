"""``AIProvider`` port.

Abstract interface for optional AI-assisted preset generation (Req 19.1). The
default implementation (Gemini) and a no-op fallback (``NullAIProvider``) live
under ``brizocast/providers/ai`` and are bound to this port by the container.
The :class:`~brizocast.core.domain.conditions.PresetParams` returned by
:meth:`generate_region_preset` shares the exact shape of a static preset, so AI
and static presets are interchangeable everywhere (Req 19.4, 19.5, 16.10). The
scoring and notification engines never reference this port (Req 19.10).

Import-light: depends only on the pure :class:`PresetParams` value object and
``typing``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from brizocast.core.domain.conditions import PresetParams


@runtime_checkable
class AIProvider(Protocol):
    """Generates region presets via an AI backend, when available.

    :meth:`is_available` reports whether the provider is usable (a disabled or
    unkeyed provider returns ``False`` so callers fall back to static presets,
    Req 15.8, 19.8). :meth:`generate_region_preset` returns a preset in the
    shared :class:`PresetParams` shape.
    """

    key: str

    def is_available(self) -> bool:
        """Return whether this provider can currently generate presets."""
        ...

    async def generate_region_preset(
        self, region: str, activity_key: str
    ) -> PresetParams:
        """Generate a default preset for ``region`` and ``activity_key``."""
        ...
