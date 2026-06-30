"""Subscriptions list route (Req 3.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from brizocast.admin import queries
from brizocast.admin.dependencies import get_session_factory
from brizocast.admin.rendering import render
from brizocast.core.container import SessionFactory

router = APIRouter()


@router.get("/subscriptions", response_class=HTMLResponse)
async def list_subscriptions(
    request: Request,
    session_factory: SessionFactory = Depends(get_session_factory),
) -> Response:
    """List every subscription with owner, activity, location, radius, mode (Req 3.1)."""
    rows = await queries.list_subscriptions(session_factory)
    return render(
        request,
        "subscriptions_list.html",
        {
            "subscriptions": rows,
            "active_page": "subscriptions",
            "title": "Subscriptions",
        },
    )
