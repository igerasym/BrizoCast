"""SQLAlchemy ORM models defining the normalized relational schema.

Importing this package imports every model module, which registers all tables
on ``Base.metadata``. Downstream code (session bootstrap, repositories,
migrations) can therefore rely on ``from brizocast.models import Base`` and
have the full schema available on ``Base.metadata``.
"""

from __future__ import annotations

from brizocast.models.activity import Activity
from brizocast.models.admin_command import AdminCommand, AdminCommandStatus
from brizocast.models.base import Base, CreatedAtMixin, TimestampMixin, utcnow
from brizocast.models.config_override import ConfigOverride
from brizocast.models.custom_condition import CustomCondition
from brizocast.models.feedback import Feedback, FeedbackRating
from brizocast.models.forecast_cache import ForecastCache
from brizocast.models.location import Location
from brizocast.models.notification import NotificationSent
from brizocast.models.payment import PaymentRecord
from brizocast.models.plan import Plan, PlanStatus, PlanTier
from brizocast.models.preset import Preset
from brizocast.models.scheduler_run import SchedulerRun
from brizocast.models.subscription import (
    DEFAULT_SEARCH_RADIUS_KM,
    Subscription,
)
from brizocast.models.surf_spot import SurfSpot
from brizocast.models.user import User

__all__ = [
    "DEFAULT_SEARCH_RADIUS_KM",
    "Activity",
    "AdminCommand",
    "AdminCommandStatus",
    "Base",
    "ConfigOverride",
    "CreatedAtMixin",
    "CustomCondition",
    "Feedback",
    "FeedbackRating",
    "ForecastCache",
    "Location",
    "NotificationSent",
    "PaymentRecord",
    "Plan",
    "PlanStatus",
    "PlanTier",
    "Preset",
    "SchedulerRun",
    "Subscription",
    "SurfSpot",
    "TimestampMixin",
    "User",
    "utcnow",
]
