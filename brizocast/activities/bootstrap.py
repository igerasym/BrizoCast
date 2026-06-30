"""Built-in activity registration (the multi-sport extension point).

:func:`register_builtin_activities` is the single place that wires the bundled
activities into the :class:`~brizocast.activities.registry.ActivityRegistry`.
The application's composition root calls it once at startup so every activity is
discoverable thereafter.

Adding a future sport means appending one ``register()`` call here and adding
its ``activities/<sport>/`` package — no existing activity, scorer, or schema is
modified (Req 17.3).
"""

from __future__ import annotations

from brizocast.activities.registry import ActivityRegistry
from brizocast.activities.surf.activity import SurfActivity


def register_builtin_activities() -> None:
    """Register every activity bundled with BrizoCast.

    Idempotent: each activity registers under its stable ``key``, so calling
    this more than once (e.g. across test setups) simply re-registers the same
    keys without duplicating entries.
    """

    ActivityRegistry.register(SurfActivity())
