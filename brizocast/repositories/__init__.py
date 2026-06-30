"""SQLAlchemy repository implementations isolating persistence from the service layer.

Each repository implements a port Protocol from
:mod:`brizocast.core.ports.repositories` and is constructed with a caller-owned
:class:`~sqlalchemy.ext.asyncio.AsyncSession` (the unit-of-work boundary; see
:mod:`brizocast.repositories.base`). Services depend only on the ports, so the
storage backing can change without touching application logic (Req 16.3).
"""

from __future__ import annotations

from brizocast.repositories.base import SqlAlchemyRepository
from brizocast.repositories.condition_repo import SqlAlchemyCustomConditionRepository
from brizocast.repositories.feedback_repo import SqlAlchemyFeedbackRepository
from brizocast.repositories.json_spot_repo import JsonSpotRepository, SpotDatasetError
from brizocast.repositories.location_repo import SqlAlchemyLocationRepository
from brizocast.repositories.notification_repo import SqlAlchemyNotificationRepository
from brizocast.repositories.payment_repo import SqlAlchemyPaymentRepository
from brizocast.repositories.plan_repo import SqlAlchemyPlanRepository
from brizocast.repositories.preset_repo import SqlAlchemyPresetRepository
from brizocast.repositories.subscription_repo import SqlAlchemySubscriptionRepository
from brizocast.repositories.user_repo import SqlAlchemyUserRepository

__all__ = [
    "JsonSpotRepository",
    "SpotDatasetError",
    "SqlAlchemyCustomConditionRepository",
    "SqlAlchemyFeedbackRepository",
    "SqlAlchemyLocationRepository",
    "SqlAlchemyNotificationRepository",
    "SqlAlchemyPaymentRepository",
    "SqlAlchemyPlanRepository",
    "SqlAlchemyPresetRepository",
    "SqlAlchemyRepository",
    "SqlAlchemySubscriptionRepository",
    "SqlAlchemyUserRepository",
]
