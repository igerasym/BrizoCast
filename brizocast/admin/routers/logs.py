"""Admin panel: Logs viewer + runtime log-level control.

Shows the last N lines of the bot's log file (data/brizocast.log by default;
configured via LOG_FILE) and auto-refreshes every few seconds. Also lets the
operator change the running bot's log level at runtime: the selection is
persisted to the shared config_overrides table and the bot applies it within a
minute via its log-level-sync job — no restart needed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from brizocast.admin.dependencies import get_config_admin_service, get_overrides
from brizocast.admin.flash import require_csrf, set_flash
from brizocast.admin.rendering import render
from brizocast.config.overrides import VALID_LOG_LEVELS, OverrideAwareSettings
from brizocast.services.config_admin_service import (
    ConfigAdminService,
    ConfigValidationError,
)

router = APIRouter(prefix="/logs", tags=["logs"])

_MAX_LINES: Final[int] = 300
_DEFAULT_LOG_FILE: Final[str] = "data/brizocast.log"
# Ordered levels for the dropdown (most to least verbose).
_LEVEL_ORDER: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _get_log_path() -> Path:
    """Resolve the log file path from settings (falls back to env/default)."""
    try:
        from brizocast.config.settings import load_settings

        return Path(load_settings().LOG_FILE)
    except Exception:  # noqa: BLE001 - fall back if settings can't load.
        return Path(os.environ.get("LOG_FILE", _DEFAULT_LOG_FILE))


def _tail_file(path: Path, n: int = _MAX_LINES) -> list[str]:
    """Read the last n lines from a file. Returns [] if file doesn't exist."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-n:]
    except OSError:
        return []


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def logs_page(
    request: Request,
    overrides: OverrideAwareSettings = Depends(get_overrides),
) -> HTMLResponse:
    """Render the logs viewer page with the current effective log level."""
    log_path = _get_log_path()
    lines = _tail_file(log_path)
    current_level = await overrides.log_level()
    return render(
        request,
        "logs.html",
        {
            "lines": lines,
            "max_lines": _MAX_LINES,
            "log_file": str(log_path),
            "file_exists": log_path.exists(),
            "current_level": current_level,
            "levels": [lvl for lvl in _LEVEL_ORDER if lvl in VALID_LOG_LEVELS],
            "active_page": "logs",
            "title": "Logs",
        },
    )


@router.get("/tail", response_class=HTMLResponse, include_in_schema=False)
async def logs_tail(request: Request) -> HTMLResponse:
    """HTMX partial: return only the log lines for live refresh."""
    log_path = _get_log_path()
    lines = _tail_file(log_path)
    content = "".join(lines) if lines else "(no log entries yet)"
    # Escape HTML so log content can't break the page.
    import html

    return HTMLResponse(content=f"<pre id='log-content'>{html.escape(content)}</pre>")


@router.post("/level", dependencies=[Depends(require_csrf)], include_in_schema=False)
async def set_level(
    level: str = Form(...),
    config: ConfigAdminService = Depends(get_config_admin_service),
) -> RedirectResponse:
    """Persist a new log level; the bot applies it within a minute."""
    response = RedirectResponse(url="/logs/", status_code=status.HTTP_303_SEE_OTHER)
    try:
        await config.set_log_level(level)
    except ConfigValidationError as exc:
        set_flash(response, str(exc), "error")
    else:
        set_flash(
            response,
            f"Log level set to {level.upper()} — the bot applies it within a minute.",
            "success",
        )
    return response


@router.post("/clear", dependencies=[Depends(require_csrf)], include_in_schema=False)
async def clear_logs(request: Request) -> RedirectResponse:
    """Truncate the log file."""
    response = RedirectResponse(url="/logs/", status_code=status.HTTP_303_SEE_OTHER)
    log_path = _get_log_path()
    try:
        with open(log_path, "w", encoding="utf-8"):
            pass
        set_flash(response, "Log file cleared.", "success")
    except OSError as exc:
        set_flash(response, f"Could not clear log file: {exc}", "error")
    return response
