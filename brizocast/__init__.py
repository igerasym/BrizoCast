"""BrizoCast — Outdoor Conditions Alert Bot.

A production-ready Telegram bot that monitors weather and ocean conditions for
outdoor sports and sends smart, low-noise notifications when conditions are
favorable.

The package follows Clean Architecture: the Telegram-facing layer (``bot``) and
the persistence layer depend inward on the domain (``core``, ``activities``,
``models``) and service (``services``) layers, never the reverse. External
dependencies (forecast/geocoding/AI providers, persistence) are reached only
through interfaces (ports) resolved by a dependency-injection container at
composition time.
"""

__version__ = "0.1.0"
