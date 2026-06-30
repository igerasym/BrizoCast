"""Repository ports.

Protocol interfaces that isolate persistence from the service layer (Req 16.3):
services depend on these abstractions, and the SQLAlchemy implementations under
``brizocast/repositories`` satisfy them. Keeping persistence behind ports lets
the storage backing change without touching application logic.

Typing choice
-------------
The MVP has no separate hand-rolled domain-entity layer mirroring every table;
the SQLAlchemy ORM models *are* the persisted entity shape. To type these ports
precisely against those entities **without** importing SQLAlchemy at runtime
(keeping the core import-light and engine-free), the ORM model types are
imported under ``typing.TYPE_CHECKING`` only. Combined with
``from __future__ import annotations`` (so all annotations are lazy strings),
``mypy --strict`` type-checks against the real entities while the module imports
nothing from SQLAlchemy when loaded at runtime. Where a pure domain value object
already exists (e.g. :class:`Forecast`), the port types against it directly.

Methods that touch persistence are ``async`` to match the async SQLAlchemy
session; the per-spot forecast cache exposes a small get/put surface (Req 7).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from brizocast.core.domain.forecast import Forecast

if TYPE_CHECKING:
    from brizocast.models.custom_condition import CustomCondition
    from brizocast.models.feedback import Feedback
    from brizocast.models.forecast_cache import ForecastCache
    from brizocast.models.location import Location
    from brizocast.models.notification import NotificationSent
    from brizocast.models.payment import PaymentRecord
    from brizocast.models.plan import Plan
    from brizocast.models.preset import Preset
    from brizocast.models.subscription import Subscription
    from brizocast.models.user import User


@runtime_checkable
class UserRepository(Protocol):
    """Persists users keyed by Telegram user id (Req 1.7)."""

    async def add(self, user: User) -> User:
        """Persist a new user and return the stored entity."""
        ...

    async def get(self, user_id: int) -> User | None:
        """Return the user with internal id ``user_id``, or ``None``."""
        ...

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        """Return the user with the given Telegram id, or ``None``."""
        ...

    async def update(self, user: User) -> None:
        """Persist changes to an existing user."""
        ...


@runtime_checkable
class PlanRepository(Protocol):
    """Persists per-user monetization plans (Req 20.*)."""

    async def add(self, plan: Plan) -> Plan:
        """Persist a new plan and return the stored entity."""
        ...

    async def get_for_user(self, user_id: int) -> Plan | None:
        """Return the plan owned by ``user_id``, or ``None``."""
        ...

    async def list_paid_active_expired(self, now: datetime) -> list[Plan]:
        """Return active Paid plans whose ``expiry_at`` is earlier than ``now``.

        These are exactly the plans the periodic plan-expiry check must flip to
        :attr:`~brizocast.models.plan.PlanStatus.EXPIRED` (Req 20.7).
        """
        ...

    async def update(self, plan: Plan) -> None:
        """Persist changes to an existing plan."""
        ...


@runtime_checkable
class PaymentRepository(Protocol):
    """Persists billing transactions (reserved for future payment integration)."""

    async def add(self, payment: PaymentRecord) -> PaymentRecord:
        """Persist a new payment record and return the stored entity."""
        ...

    async def list_for_plan(self, plan_id: int) -> list[PaymentRecord]:
        """Return all payment records associated with ``plan_id``."""
        ...


@runtime_checkable
class LocationRepository(Protocol):
    """Persists user locations and favorites (Req 2.*)."""

    async def add(self, location: Location) -> Location:
        """Persist a new location and return the stored entity."""
        ...

    async def get(self, location_id: int) -> Location | None:
        """Return the location with id ``location_id``, or ``None``."""
        ...

    async def list_for_user(self, user_id: int) -> list[Location]:
        """Return every location owned by ``user_id``."""
        ...

    async def list_favorites(self, user_id: int) -> list[Location]:
        """Return the user's saved favorite locations."""
        ...

    async def delete(self, location_id: int) -> None:
        """Remove the location with id ``location_id``."""
        ...


@runtime_checkable
class SubscriptionRepository(Protocol):
    """Persists user subscriptions (Req 3.*)."""

    async def add(self, sub: Subscription) -> Subscription:
        """Persist a new subscription and return the stored entity."""
        ...

    async def get(self, sub_id: int) -> Subscription | None:
        """Return the subscription with id ``sub_id``, or ``None``."""
        ...

    async def list_for_user(self, user_id: int) -> list[Subscription]:
        """Return every subscription owned by ``user_id``."""
        ...

    async def list_all_active(self) -> list[Subscription]:
        """Return every active subscription across all users."""
        ...

    async def update(self, sub: Subscription) -> None:
        """Persist changes to an existing subscription."""
        ...

    async def delete(self, sub_id: int) -> None:
        """Remove the subscription with id ``sub_id``."""
        ...

    async def count_for_user(self, user_id: int) -> int:
        """Return the number of subscriptions owned by ``user_id``."""
        ...


@runtime_checkable
class PresetRepository(Protocol):
    """Persists default/region and user-custom presets (Req 4.*, 19.*)."""

    async def add(self, preset: Preset) -> Preset:
        """Persist a new preset and return the stored entity."""
        ...

    async def get(self, preset_id: int) -> Preset | None:
        """Return the preset with id ``preset_id``, or ``None``."""
        ...

    async def list_defaults(self, region: str | None = None) -> list[Preset]:
        """Return default presets, optionally filtered to ``region``."""
        ...

    async def list_for_user(self, user_id: int) -> list[Preset]:
        """Return the custom presets owned by ``user_id``."""
        ...

    async def update(self, preset: Preset) -> None:
        """Persist changes to an existing preset."""
        ...


@runtime_checkable
class CustomConditionRepository(Protocol):
    """Persists per-subscription custom condition overrides (Req 4.5-4.7)."""

    async def add(self, condition: CustomCondition) -> CustomCondition:
        """Persist new custom conditions and return the stored entity."""
        ...

    async def get_for_subscription(
        self, subscription_id: int
    ) -> CustomCondition | None:
        """Return the custom conditions for ``subscription_id``, or ``None``."""
        ...

    async def update(self, condition: CustomCondition) -> None:
        """Persist changes to existing custom conditions."""
        ...

    async def delete(self, subscription_id: int) -> None:
        """Remove the custom conditions bound to ``subscription_id``."""
        ...


@runtime_checkable
class NotificationRepository(Protocol):
    """Persists sent-notification records for anti-spam and digests (Req 9.*)."""

    async def add(self, record: NotificationSent) -> NotificationSent:
        """Persist a new notification record and return the stored entity."""
        ...

    async def latest(
        self, subscription_id: int, spot_key: str, forecast_window_key: str
    ) -> NotificationSent | None:
        """Return the most recent record for the (sub, spot, window) identity."""
        ...

    async def list_since(
        self, subscription_id: int, since: datetime
    ) -> list[NotificationSent]:
        """Return records for ``subscription_id`` sent at/after ``since``."""
        ...


@runtime_checkable
class FeedbackRepository(Protocol):
    """Persists user thumbs-up/down feedback on alerts (Req 12.4, 12.5)."""

    async def add(self, feedback: Feedback) -> Feedback:
        """Persist a new feedback entry and return the stored entity."""
        ...

    async def list_for_subscription(self, subscription_id: int) -> list[Feedback]:
        """Return all feedback recorded for ``subscription_id``."""
        ...


@runtime_checkable
class ForecastCacheRepository(Protocol):
    """Persists per-spot cached forecasts shared across subscriptions (Req 7)."""

    async def get(self, spot_key: str) -> ForecastCache | None:
        """Return the cached entry for ``spot_key``, or ``None`` if absent."""
        ...

    async def put(
        self,
        spot_key: str,
        forecast: Forecast,
        fetched_at: datetime,
        expires_at: datetime,
    ) -> None:
        """Store ``forecast`` for ``spot_key`` with fetch/expiry timestamps."""
        ...

    async def delete_expired(self, now: datetime) -> int:
        """Remove entries expired as of ``now``; return the number removed."""
        ...
