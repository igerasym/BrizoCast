"""Users list/detail and plan-change routes (Req 2.*, 3.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from brizocast.admin import queries
from brizocast.admin.dependencies import get_session_factory, get_subscription_service
from brizocast.admin.flash import require_csrf, set_flash
from brizocast.admin.rendering import render
from brizocast.core.container import SessionFactory
from brizocast.database.session import session_scope
from brizocast.models.plan import Plan, PlanTier
from brizocast.services.subscription_service import SubscriptionService

router = APIRouter()


@router.get("/users", response_class=HTMLResponse)
async def list_users(
    request: Request,
    session_factory: SessionFactory = Depends(get_session_factory),
) -> Response:
    """List every user with Telegram id, plan, and subscription count (Req 2.1)."""
    rows = await queries.list_users(session_factory)
    return render(
        request,
        "users_list.html",
        {"users": rows, "active_page": "users", "title": "Users"},
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(
    request: Request,
    user_id: int,
    session_factory: SessionFactory = Depends(get_session_factory),
    subscriptions: SubscriptionService = Depends(get_subscription_service),
) -> Response:
    """Show a user's profile, plan, and subscriptions; 404 if absent (Req 2.2, 2.5, 3.2)."""
    detail = await queries.get_user_detail(session_factory, user_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    subs = await subscriptions.summarize_for_user(user_id)
    return render(
        request,
        "user_detail.html",
        {
            "user": detail,
            "subscriptions": subs,
            "tiers": [tier.value for tier in PlanTier],
            "active_page": "users",
            "title": f"User {detail.telegram_user_id}",
        },
    )


@router.post("/users/{user_id}/plan", dependencies=[Depends(require_csrf)])
async def change_plan(
    request: Request,
    user_id: int,
    tier: str = Form(...),
    session_factory: SessionFactory = Depends(get_session_factory),
) -> RedirectResponse:
    """Change a user's plan tier to Free or Paid and confirm (Req 2.3, 2.4)."""
    response = RedirectResponse(
        url=f"/users/{user_id}", status_code=status.HTTP_303_SEE_OTHER
    )
    try:
        new_tier = PlanTier(tier)
    except ValueError:
        set_flash(response, f"Unknown plan tier {tier!r}.", "error")
        return response

    async with session_scope(session_factory) as session:
        plan = (
            await session.execute(select(Plan).where(Plan.user_id == user_id))
        ).scalar_one_or_none()
        if plan is None:
            set_flash(response, "User has no plan to change.", "error")
            return response
        plan.tier = new_tier

    set_flash(response, f"Plan updated to {new_tier.value}.", "success")
    return response
