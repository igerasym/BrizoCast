"""Feedback list route with up/down totals (Req 10.1, 10.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from brizocast.admin import queries
from brizocast.admin.dependencies import get_session_factory
from brizocast.admin.rendering import render
from brizocast.core.container import SessionFactory

router = APIRouter()


@router.get("/feedback", response_class=HTMLResponse)
async def list_feedback(
    request: Request,
    session_factory: SessionFactory = Depends(get_session_factory),
) -> Response:
    """List feedback entries and the thumbs-up/thumbs-down totals (Req 10.1, 10.2)."""
    view = await queries.list_feedback(session_factory)
    return render(
        request,
        "feedback_list.html",
        {"feedback": view, "active_page": "feedback", "title": "Feedback"},
    )
