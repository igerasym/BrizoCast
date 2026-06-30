"""Application configuration loaded from a ``.env`` file and validated by Pydantic.

This module is the single source of truth for runtime configuration. It defines
the :class:`Settings` model (a ``pydantic-settings`` ``BaseSettings``), the
:class:`PlanLimit` model that describes monetization quotas per plan tier, and a
:func:`load_settings` helper that validates configuration at startup and fails
loudly — naming the offending field — when a required value is missing or
invalid.

The module is intentionally framework-free apart from Pydantic: it has no
dependency on Telegram, SQLAlchemy, APScheduler, or any provider. Logging uses
the standard library ``logging`` module so configuration loading never blocks on
the project's structured-logging setup.

Requirements covered: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.9, 15.10.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Notification mode keys (mirror Requirement 10.1). Represented here as plain
# strings to keep the configuration layer decoupled from the notification
# module, which defines the corresponding ``NotificationMode`` enum. The two
# share these stable string keys.
NOTIFICATION_MODE_IMMEDIATE = "immediate"
NOTIFICATION_MODE_MORNING_DIGEST = "morning_digest"
NOTIFICATION_MODE_EVENING_DIGEST = "evening_digest"
NOTIFICATION_MODE_WEEKLY_BEST_DAY = "weekly_best_day"

ALL_NOTIFICATION_MODES: frozenset[str] = frozenset(
    {
        NOTIFICATION_MODE_IMMEDIATE,
        NOTIFICATION_MODE_MORNING_DIGEST,
        NOTIFICATION_MODE_EVENING_DIGEST,
        NOTIFICATION_MODE_WEEKLY_BEST_DAY,
    }
)

# Plan tier keys (mirror the Plan_Tier glossary entry: Free or Paid). Used as the
# keys of :attr:`Settings.PLAN_LIMITS` and matched against a user's plan tier by
# the entitlement service.
PLAN_TIER_FREE = "free"
PLAN_TIER_PAID = "paid"


class PlanLimit(BaseModel):
    """Quota values associated with a single :term:`Plan_Tier`.

    Carries the maximum number of subscriptions a user on the tier may own and
    the set of notification modes available to that tier (Req 15.9, 21.1).
    """

    max_subscriptions: int = Field(
        ...,
        ge=1,
        description="Maximum number of subscriptions a user on this tier may own.",
    )
    notification_modes: set[str] = Field(
        ...,
        description="Notification mode keys available to this tier.",
    )


def default_plan_limits() -> dict[str, PlanLimit]:
    """Return the default :class:`PlanLimit` map for each plan tier.

    The Free tier is intentionally limited (few subscriptions, digest-light) and
    the Paid tier is generous with access to every notification mode. These
    defaults only take effect when ``MONETIZATION_ENABLED`` is true; while
    monetization is disabled every user is treated as fully entitled (Req 15.10).
    """

    return {
        PLAN_TIER_FREE: PlanLimit(
            max_subscriptions=2,
            notification_modes={
                NOTIFICATION_MODE_IMMEDIATE,
                NOTIFICATION_MODE_MORNING_DIGEST,
            },
        ),
        PLAN_TIER_PAID: PlanLimit(
            max_subscriptions=50,
            notification_modes=set(ALL_NOTIFICATION_MODES),
        ),
    }


class Settings(BaseSettings):
    """Validated application configuration sourced from the environment / ``.env``.

    Field names map directly to environment variable names (case-insensitive).
    Defaults satisfy the configuration requirements: an unspecified forecast
    provider resolves to Open-Meteo Marine (Req 15.5), an enabled AI provider
    with no explicit choice resolves to Gemini (Req 15.7), and an unspecified
    monetization flag defaults to disabled (Req 15.10).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram & core ---------------------------------------------------- #
    TELEGRAM_BOT_TOKEN: str
    DATABASE_URL: str = "sqlite+aiosqlite:///data/brizocast.db"
    SCHEDULER_INTERVAL_MINUTES: int = Field(default=60, ge=1)

    # --- Forecast / geocoding ---------------------------------------------- #
    FORECAST_PROVIDER: str = "open_meteo_marine"  # default (Req 15.5)
    GEOCODING_PROVIDER: str = "open_meteo_geocoding"
    FORECAST_CACHE_TTL_MINUTES: int = Field(default=180, ge=1)

    # Shared surf-spot dataset on the ./data volume. Both the bot and the admin
    # panel point their JsonSpotRepository here so spot edits are shared
    # (Req 4.1, 14.2). Seeded from the bundled resource on first startup.
    SPOT_DATASET_PATH: str = "data/surf_spots.json"

    # When enabled, sharing/searching a location imports nearby named spots from
    # the spot catalogue (Surfline) into the shared dataset. Degrades gracefully
    # if the catalogue is unavailable. SPOT_INGEST_RADIUS_KM is the import area.
    SPOT_INGEST_ENABLED: bool = True
    SPOT_INGEST_RADIUS_KM: int = Field(default=50, ge=1)

    # --- Notifications ------------------------------------------------------ #
    SIGNIFICANT_IMPROVEMENT: int = Field(default=10, ge=0)  # score points (Req 9.6)
    NOTIFY_RETRY_COUNT: int = Field(default=3, ge=0)
    MORNING_DIGEST_TIME: str = "07:00"
    EVENING_DIGEST_TIME: str = "18:00"
    WEEKLY_DIGEST: str = "MON 07:00"

    # --- AI (optional) ------------------------------------------------------ #
    AI_ENABLED: bool = False
    AI_PROVIDER: str = "gemini"  # default (Req 15.7)
    AI_API_KEY: str | None = None
    AI_MODEL: str = "gemini-1.5-flash"

    # --- Monetization ------------------------------------------------------- #
    MONETIZATION_ENABLED: bool = False  # default disabled (Req 15.10)
    PLAN_LIMITS: dict[str, PlanLimit] = Field(default_factory=default_plan_limits)


def _format_validation_errors(error: ValidationError) -> list[str]:
    """Build human-readable ``FIELD: message`` strings from a ``ValidationError``."""

    messages: list[str] = []
    for err in error.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        messages.append(f"{location}: {err['msg']}")
    return messages


def load_settings(_env_file: str | None = None) -> Settings:
    """Load and validate :class:`Settings`, failing startup on invalid config.

    On a missing or invalid required value, every offending field is logged by
    name (Req 15.4) and the underlying ``ValidationError`` is re-raised so that
    application startup terminates (Req 15.3, 15.4).

    Args:
        _env_file: Optional override for the ``.env`` file path. Primarily used
            by tests; when ``None`` the path from :class:`Settings.model_config`
            (``.env``) is used.
    """

    try:
        if _env_file is not None:
            return Settings(_env_file=_env_file)  # type: ignore[call-arg]
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        offending = _format_validation_errors(exc)
        logger.error(
            "Configuration validation failed; the following Configuration "
            "value(s) are missing or invalid: %s",
            "; ".join(offending),
        )
        for message in offending:
            logger.error("Invalid Configuration field -> %s", message)
        raise
