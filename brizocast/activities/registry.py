"""The :class:`ActivityRegistry` — a registry open for extension.

Maps an activity ``key`` to its :class:`~brizocast.activities.base.Activity`
instance. The registry is the single lookup point the scheduler uses to select
the :class:`~brizocast.core.ports.scorer.Scorer` for a subscription's activity
(``ActivityRegistry.get(sub.activity_key).scorer()``, Req 17.6), and the only
place a new sport is plugged in (``register()``, Req 17.3) — adding an activity
never requires modifying an existing activity's implementation.

The registry stores activities in a class-level (process-global) mapping, so a
single registration during application bootstrap makes an activity discoverable
everywhere.
"""

from __future__ import annotations

from typing import Any

from brizocast.activities.base import Activity
from brizocast.core.errors import NotFoundError


class ActivityRegistry:
    """Process-global registry of supported activities, keyed by ``key``.

    The condition type of a stored activity is erased to ``Any`` here: the
    registry treats activities uniformly, while each concrete activity keeps its
    own precise condition typing internally.
    """

    _items: dict[str, Activity[Any]] = {}

    @classmethod
    def register(cls, activity: Activity[Any]) -> None:
        """Register ``activity`` under its ``key`` (replacing any prior entry).

        Idempotent for a given activity type: re-registering the same key simply
        overwrites the previous instance.
        """

        cls._items[activity.key] = activity

    @classmethod
    def get(cls, key: str) -> Activity[Any]:
        """Return the activity registered under ``key``.

        Raises:
            NotFoundError: if no activity is registered for ``key``.
        """

        try:
            return cls._items[key]
        except KeyError:
            raise NotFoundError(f"No activity registered for key {key!r}") from None

    @classmethod
    def all(cls) -> list[Activity[Any]]:
        """Return every registered activity, available in the MVP or not."""

        return list(cls._items.values())

    @classmethod
    def available(cls) -> list[Activity[Any]]:
        """Return only activities marked ``available_in_mvp`` (Req 1.3)."""

        return [activity for activity in cls._items.values() if activity.available_in_mvp]
