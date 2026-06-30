"""Smoke test for the composition root (task 11.1, Req 13.1, 15.1, 16.4).

Verifies that :func:`brizocast.bot.app.build_application` assembles a fully-wired
``python-telegram-bot`` ``Application`` — registering every supported command
and conversation handler with the unknown-command fallback **last** — and that
the database bootstrap + activity seeding run, **without** starting long
polling, the scheduler, or making any network/Telegram call.

It uses a temporary file-backed SQLite database and a dummy bot token. No
``run_polling`` is invoked; the loop-bound steps normally driven by ``post_init``
(``bootstrap_database`` and activity seeding) are exercised directly here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)

from brizocast.bot.app import _seed_activities, build_application
from brizocast.bot.handlers.subscriptions import CTX_ACTIVITY_IDS
from brizocast.config.settings import Settings
from brizocast.database.bootstrap import bootstrap_database
from brizocast.database.session import create_engine, create_session_factory

# The commands Req 13.1 requires the bot to support.
_REQUIRED_COMMANDS = {
    "start",
    "location",
    "subscriptions",
    "add",
    "settings",
    "presets",
    "status",
    "forecast",
    "help",
}


def _settings(tmp_path: Path) -> Settings:
    """Build settings pointing at a throwaway file-backed SQLite database."""
    db_path = tmp_path / "brizocast.db"
    return Settings(
        TELEGRAM_BOT_TOKEN="123456:dummy-token-for-tests",
        DATABASE_URL=f"sqlite+aiosqlite:///{db_path}",
    )


def _registered_commands(application: object) -> set[str]:
    """Collect every command trigger registered on the application."""
    commands: set[str] = set()
    for group in application.handlers.values():  # type: ignore[attr-defined]
        for handler in group:
            if isinstance(handler, CommandHandler):
                commands |= set(handler.commands)
            elif isinstance(handler, ConversationHandler):
                for entry in handler.entry_points:
                    if isinstance(entry, CommandHandler):
                        commands |= set(entry.commands)
    return commands


@pytest.mark.integration
def test_build_application_registers_required_commands(tmp_path: Path) -> None:
    """The composition root registers all Req 13.1 commands and the feedback callback."""
    application = build_application(_settings(tmp_path))

    commands = _registered_commands(application)
    assert _REQUIRED_COMMANDS <= commands

    # The 👍/👎 feedback callback handler is wired (Req 12.3).
    has_callback_handler = any(
        isinstance(handler, CallbackQueryHandler)
        for group in application.handlers.values()
        for handler in group
    )
    assert has_callback_handler


@pytest.mark.integration
def test_unknown_command_fallback_registered_last(tmp_path: Path) -> None:
    """The catch-all unknown-command fallback is the final registered handler (Req 13.7)."""
    application = build_application(_settings(tmp_path))

    last_group = max(application.handlers)
    last_handler = application.handlers[last_group][-1]
    # The fallback is a MessageHandler on filters.COMMAND; it must be last so it
    # never shadows the real command handlers.
    assert isinstance(last_handler, MessageHandler)


@pytest.mark.integration
async def test_bootstrap_creates_schema_and_seeds_activities(tmp_path: Path) -> None:
    """post_init's bootstrap + activity seeding create the schema and Surf row (Req 16.4)."""
    settings = _settings(tmp_path)
    # Building the application registers the built-in activities.
    build_application(settings)

    engine = create_engine(settings.DATABASE_URL)
    session_factory = create_session_factory(engine)
    try:
        await bootstrap_database(engine)
        activity_ids = await _seed_activities(session_factory)
    finally:
        await engine.dispose()

    assert "surf" in activity_ids
    assert isinstance(activity_ids["surf"], int)


@pytest.mark.integration
async def test_activity_seeding_is_idempotent(tmp_path: Path) -> None:
    """Seeding twice reuses the same activity rows (no duplicates) (Req 16.4 wiring)."""
    settings = _settings(tmp_path)
    build_application(settings)

    engine = create_engine(settings.DATABASE_URL)
    session_factory = create_session_factory(engine)
    try:
        await bootstrap_database(engine)
        first = await _seed_activities(session_factory)
        second = await _seed_activities(session_factory)
    finally:
        await engine.dispose()

    assert first == second
    # And the published map key matches what the subscription handlers read.
    assert CTX_ACTIVITY_IDS == "activity_ids"
