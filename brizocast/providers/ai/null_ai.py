"""``NullAIProvider`` — the no-op / fallback :class:`AIProvider`.

This implementation is used whenever AI-assisted preset generation is *not*
available: when ``AI_ENABLED`` is false, when an AI provider is enabled but has
no API key configured (the enabled-but-unkeyed fallback, Req 15.8), or when the
configured AI provider key has no registered implementation yet (e.g. the Gemini
implementation, task 9.1, has not been registered).

It satisfies the :class:`~brizocast.core.ports.ai_provider.AIProvider` port so
the rest of the system can treat the "no AI" case uniformly: callers check
:meth:`is_available` (which always returns ``False``) before ever calling
:meth:`generate_region_preset`, and fall back to bundled static
``Default_Presets`` (Req 19.8). If :meth:`generate_region_preset` is called
anyway it raises a clear :class:`ProviderRequestError` rather than returning a
bogus preset.

Import-light: depends only on the pure ``PresetParams`` value object and the
domain error hierarchy — no Telegram, SQLAlchemy, HTTP, or AI-SDK imports.
"""

from __future__ import annotations

from brizocast.core.domain.conditions import PresetParams
from brizocast.core.errors import ProviderRequestError

__all__ = ["NullAIProvider"]


class NullAIProvider:
    """A no-op :class:`AIProvider` that is never available.

    Used as the fallback when AI preset generation is disabled, unkeyed, or
    unregistered. :meth:`is_available` always returns ``False`` so well-behaved
    callers never invoke :meth:`generate_region_preset`; if they do, a clear
    :class:`ProviderRequestError` is raised.
    """

    #: Stable provider key (matches the factory's fallback selection). Declared
    #: as a plain settable attribute to satisfy the ``AIProvider`` port, whose
    #: ``key`` is a writable protocol member.
    key: str = "null"

    def is_available(self) -> bool:
        """Return ``False`` — this provider can never generate presets."""
        return False

    async def generate_region_preset(
        self, region: str, activity_key: str
    ) -> PresetParams:
        """Always fail: the null provider cannot generate presets.

        Callers must consult :meth:`is_available` first and fall back to static
        presets; reaching here indicates a programming error.

        Raises:
            ProviderRequestError: Always, since no AI backend is available.
        """
        raise ProviderRequestError(
            "AI preset generation is unavailable; the NullAIProvider cannot "
            f"generate a preset for region {region!r} / activity "
            f"{activity_key!r}. Check is_available() and fall back to static "
            "Default_Presets.",
            provider=self.key,
        )
