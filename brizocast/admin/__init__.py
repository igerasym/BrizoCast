"""BrizoCast Admin web panel package.

The ``brizocast.admin`` package holds the server-rendered FastAPI + Jinja2/HTMX
web administration interface that runs as a separate Docker Compose service
alongside the Telegram bot. It reuses the bot's service and repository layer
through its own dependency-injection container pointed at the shared SQLite
database and ``./data`` volume; nothing under ``admin/`` is imported by the bot.
"""

from __future__ import annotations
