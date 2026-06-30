"""Admin panel configuration loaded from a ``.env`` file and validated by Pydantic.

This module defines :class:`PanelSettings` — a ``pydantic-settings``
``BaseSettings`` model that is intentionally separate from the bot's
:class:`brizocast.config.settings.Settings`, so the panel can boot without the
bot's required ``TELEGRAM_BOT_TOKEN`` while still reading the *shared*
``DATABASE_URL`` and surf-spot dataset path.

:func:`load_panel_settings` mirrors :func:`brizocast.config.settings.load_settings`:
on a missing or invalid required value (notably ``ADMIN_USERNAME`` /
``ADMIN_PASSWORD``) it logs the offending field by name and terminates startup by
raising :class:`SystemExit` with code ``1``.

Security note: ``ADMIN_BIND_HOST`` defaults to the loopback address
``127.0.0.1`` and is intended to be set to the Raspberry Pi's LAN address — never
``0.0.0.0`` — so the panel is never published on a public, all-interfaces
address.

Requirements covered: 13.1, 13.2, 13.3, 13.4.
"""

from __future__ import annotations

import logging

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class PanelSettings(BaseSettings):
    """Validated admin-panel configuration sourced from the environment / ``.env``.

    Field names map directly to environment variable names (case-insensitive).
    ``ADMIN_USERNAME`` and ``ADMIN_PASSWORD`` are required (Req 13.2); the
    remaining fields carry safe LAN-only defaults. ``DATABASE_URL`` defaults to
    the shared SQLite database and ``SPOT_DATASET_PATH`` to the shared surf-spot
    JSON dataset on the ``./data`` volume (Req 13.4).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    ADMIN_USERNAME: str  # Req 13.2, 13.3
    ADMIN_PASSWORD: str  # Req 13.2, 13.3
    # LAN host; never 0.0.0.0 (Req 13.2, 1.5).
    ADMIN_BIND_HOST: str = "127.0.0.1"
    ADMIN_PORT: int = Field(default=8000, ge=1, le=65535)  # Req 13.2
    # Shared DB on the ./data volume (Req 13.4).
    DATABASE_URL: str = "sqlite+aiosqlite:///data/brizocast.db"
    # Shared surf-spot JSON dataset on the ./data volume (Req 4.*, 14.2).
    SPOT_DATASET_PATH: str = "data/surf_spots.json"


def _format_validation_errors(error: ValidationError) -> list[str]:
    """Build human-readable ``FIELD: message`` strings from a ``ValidationError``."""

    messages: list[str] = []
    for err in error.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        messages.append(f"{location}: {err['msg']}")
    return messages


def load_panel_settings(_env_file: str | None = None) -> PanelSettings:
    """Load and validate :class:`PanelSettings`, failing startup on invalid config.

    On a missing or invalid required value (notably ``ADMIN_USERNAME`` /
    ``ADMIN_PASSWORD``), every offending field is logged by name (Req 13.3) and
    startup is terminated by raising :class:`SystemExit` with code ``1``.

    Args:
        _env_file: Optional override for the ``.env`` file path. Primarily used
            by tests; when ``None`` the path from
            :class:`PanelSettings.model_config` (``.env``) is used.

    Returns:
        The validated :class:`PanelSettings`.

    Raises:
        SystemExit: If configuration validation fails (exit code ``1``).
    """

    try:
        if _env_file is not None:
            return PanelSettings(_env_file=_env_file)  # type: ignore[call-arg]
        return PanelSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        offending = _format_validation_errors(exc)
        logger.error(
            "Panel configuration validation failed; the following Panel "
            "Configuration value(s) are missing or invalid: %s",
            "; ".join(offending),
        )
        for message in offending:
            logger.error("Invalid Panel Configuration field -> %s", message)
        raise SystemExit(1) from exc
