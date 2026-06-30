"""Run-forecast-check-now and broadcast command routes (Req 8.*, 9.*)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from brizocast.admin.dependencies import get_admin_command_service
from brizocast.admin.flash import require_csrf, set_flash
from brizocast.admin.rendering import render
from brizocast.services.admin_command_service import (
    AdminCommandService,
    AdminCommandType,
)

router = APIRouter()


def _redirect() -> RedirectResponse:
    """Return a post-action redirect back to the commands page."""
    return RedirectResponse(url="/commands", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/commands", response_class=HTMLResponse)
async def commands_page(request: Request) -> Response:
    """Render the run-check-now and broadcast forms (Req 8.*, 9.*)."""
    return render(
        request,
        "commands.html",
        {"active_page": "commands", "title": "Commands"},
    )


@router.post("/commands/run-check", dependencies=[Depends(require_csrf)])
async def run_check(
    service: AdminCommandService = Depends(get_admin_command_service),
) -> RedirectResponse:
    """Enqueue a run-forecast-check command for the bot to drain (Req 8.1, 8.2)."""
    await service.enqueue(AdminCommandType.RUN_FORECAST_CHECK)
    response = _redirect()
    set_flash(response, "Forecast-check request enqueued.", "success")
    return response


@router.post("/commands/broadcast", dependencies=[Depends(require_csrf)])
async def broadcast(
    text: str = Form(...),
    service: AdminCommandService = Depends(get_admin_command_service),
) -> RedirectResponse:
    """Enqueue a broadcast command, rejecting empty text (Req 9.1, 9.2, 9.4)."""
    response = _redirect()
    message = text.strip()
    if not message:
        set_flash(response, "Broadcast message must not be empty.", "error")
        return response
    await service.enqueue(AdminCommandType.BROADCAST, {"text": message})
    set_flash(response, "Broadcast announcement enqueued.", "success")
    return response
