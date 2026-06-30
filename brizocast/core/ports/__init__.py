"""Port interfaces (abstract base classes / Protocols) for external dependencies.

Re-exports the provider, spot, repository, and scorer ports so callers can
import them directly from ``brizocast.core.ports``.
"""

from __future__ import annotations

from brizocast.core.ports.ai_provider import AIProvider
from brizocast.core.ports.forecast_provider import ForecastProvider
from brizocast.core.ports.geocoding_provider import GeocodingProvider
from brizocast.core.ports.repositories import (
    CustomConditionRepository,
    FeedbackRepository,
    ForecastCacheRepository,
    LocationRepository,
    NotificationRepository,
    PaymentRepository,
    PlanRepository,
    PresetRepository,
    SubscriptionRepository,
    UserRepository,
)
from brizocast.core.ports.scorer import DaylightResolver, Scorer
from brizocast.core.ports.spot_repository import SpotRepository

__all__ = [
    "AIProvider",
    "CustomConditionRepository",
    "DaylightResolver",
    "FeedbackRepository",
    "ForecastCacheRepository",
    "ForecastProvider",
    "GeocodingProvider",
    "LocationRepository",
    "NotificationRepository",
    "PaymentRepository",
    "PlanRepository",
    "PresetRepository",
    "Scorer",
    "SpotRepository",
    "SubscriptionRepository",
    "UserRepository",
]
