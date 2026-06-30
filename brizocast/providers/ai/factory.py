"""AI provider factory and registry (optional preset generation).

:func:`build_ai_provider` resolves the configured AI backend to a concrete
:class:`~brizocast.core.ports.ai_provider.AIProvider`, following the design's
resolution logic:

#. ``AI_ENABLED`` is false → :class:`NullAIProvider` (AI disabled).
#. ``AI_ENABLED`` true but no ``AI_API_KEY`` → :class:`NullAIProvider`
   (the enabled-but-unkeyed fallback, Req 15.8).
#. otherwise resolve ``AI_PROVIDER`` (defaulting to ``"gemini"`` when
   unspecified, Req 15.7) against the provider **registry**.

The registry is a small ``dict`` mapping a provider key to a *builder*
(``(api_key, model) -> AIProvider``). It lets the Gemini implementation
(``providers/ai/gemini.py``) register itself under ``"gemini"`` at import time
without this module importing it at load time — keeping the import direction
clean (``gemini`` imports :func:`register_ai_provider` from here). When the
resolved key is ``"gemini"`` and not yet registered, the factory imports the
``gemini`` module *lazily* so it self-registers, then resolves it. When a
resolved key has **no** registered builder, the factory logs the situation and
falls back to :class:`NullAIProvider` so the system keeps working on bundled
static presets (Req 19.8).

Requirements covered: 6.3, 15.5, 15.7, 15.10, 19.1, 19.3 (supports Property 22).
"""

from __future__ import annotations

from collections.abc import Callable

from brizocast.config.settings import Settings
from brizocast.core.logging import get_logger
from brizocast.core.ports.ai_provider import AIProvider
from brizocast.providers.ai.null_ai import NullAIProvider

__all__ = [
    "AIProviderBuilder",
    "build_ai_provider",
    "register_ai_provider",
    "registered_ai_provider_keys",
]

# Default AI provider key used when AI is enabled but ``AI_PROVIDER`` is left
# unspecified (Req 15.7 / 19.3).
DEFAULT_AI_PROVIDER_KEY = "gemini"

_log = get_logger(__name__)

# A builder turns a (validated, non-empty) API key and model identifier into a
# concrete provider. Implementations register their builder under a key so the
# factory can resolve a configured selection without importing them directly.
AIProviderBuilder = Callable[[str, str], AIProvider]

# The provider registry. Empty until concrete implementations register; the
# Gemini implementation (task 9.1) is expected to call ``register_ai_provider``
# at import time under :data:`DEFAULT_AI_PROVIDER_KEY`.
_AI_PROVIDER_REGISTRY: dict[str, AIProviderBuilder] = {}


def register_ai_provider(key: str, builder: AIProviderBuilder) -> None:
    """Register an :class:`AIProvider` builder under ``key``.

    Intended to be called once, at import time, by a concrete provider module
    (e.g. ``providers/ai/gemini.py`` in task 9.1). Re-registering the same key
    replaces the previous builder.

    Args:
        key: The provider key matched against ``Settings.AI_PROVIDER`` (e.g.
            ``"gemini"``).
        builder: Callable building the provider from a non-empty API key and a
            model identifier.
    """
    _AI_PROVIDER_REGISTRY[key] = builder


def registered_ai_provider_keys() -> frozenset[str]:
    """Return the set of currently registered AI provider keys."""
    return frozenset(_AI_PROVIDER_REGISTRY)


def build_ai_provider(cfg: Settings) -> AIProvider:
    """Resolve the configured AI provider, falling back to :class:`NullAIProvider`.

    See the module docstring for the full resolution logic. The returned
    provider always satisfies the :class:`AIProvider` port; when AI is disabled,
    unkeyed, or unregistered the result is a :class:`NullAIProvider` whose
    :meth:`is_available` is ``False`` (Req 15.8, 19.8).

    Args:
        cfg: The validated application :class:`Settings`.

    Returns:
        A concrete :class:`AIProvider`. Never ``None``.
    """
    if not cfg.AI_ENABLED:
        # AI disabled: use static presets (Req 19.1 — optional & configurable).
        return NullAIProvider()

    if not cfg.AI_API_KEY:
        # Enabled but unkeyed: treat as unavailable and fall back (Req 15.8).
        _log.warning(
            "AI provider enabled but no AI_API_KEY configured; "
            "falling back to static presets.",
        )
        return NullAIProvider()

    # Default to Gemini when the provider is left unspecified (Req 15.7 / 19.3).
    key = cfg.AI_PROVIDER or DEFAULT_AI_PROVIDER_KEY

    builder = _AI_PROVIDER_REGISTRY.get(key)
    if builder is None and key == DEFAULT_AI_PROVIDER_KEY:
        # Lazily import the bundled Gemini implementation so it self-registers.
        # The import is deferred to here (rather than at module load) to keep
        # the import direction clean: ``gemini`` imports ``register_ai_provider``
        # from this module, so importing it at load time would create a cycle.
        from brizocast.providers.ai import gemini as _gemini  # noqa: F401

        builder = _AI_PROVIDER_REGISTRY.get(key)

    if builder is None:
        # The implementation for this key has not been registered (e.g. the
        # Gemini provider from task 9.1 is not yet wired in). Degrade to static
        # presets rather than failing startup (Req 19.8).
        _log.warning(
            "No AI provider registered under key %r (registered: %s); "
            "falling back to static presets.",
            key,
            sorted(_AI_PROVIDER_REGISTRY),
        )
        return NullAIProvider()

    return builder(cfg.AI_API_KEY, cfg.AI_MODEL)
