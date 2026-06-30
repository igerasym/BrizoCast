"""Monetization flag + plan-limit editing routes, applied live (Req 6.*)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from brizocast.admin.dependencies import get_config_admin_service, get_overrides
from brizocast.admin.flash import require_csrf, set_flash
from brizocast.admin.rendering import render
from brizocast.config.overrides import OverrideAwareSettings
from brizocast.config.settings import (
    ALL_NOTIFICATION_MODES,
    PLAN_TIER_FREE,
    PLAN_TIER_PAID,
    PlanLimit,
)
from brizocast.services.config_admin_service import (
    ConfigAdminService,
    ConfigValidationError,
)

router = APIRouter()


def _parse_modes(raw: str) -> set[str]:
    """Parse a comma-separated notification-mode list into a set of keys."""
    return {part.strip() for part in raw.split(",") if part.strip()}


def _redirect() -> RedirectResponse:
    """Return a post-action redirect back to the monetization page."""
    return RedirectResponse(
        url="/monetization", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/monetization", response_class=HTMLResponse)
async def monetization_page(
    request: Request,
    overrides: OverrideAwareSettings = Depends(get_overrides),
) -> Response:
    """Show the current flag and per-tier plan limits (Req 6.1)."""
    enabled = await overrides.monetization_enabled()
    limits = await overrides.plan_limits()
    return render(
        request,
        "monetization.html",
        {
            "enabled": enabled,
            "limits": limits,
            "tiers": [PLAN_TIER_FREE, PLAN_TIER_PAID],
            "all_modes": sorted(ALL_NOTIFICATION_MODES),
            "active_page": "monetization",
            "title": "Monetization",
        },
    )


@router.post("/monetization/flag", dependencies=[Depends(require_csrf)])
async def set_flag(
    enabled: str = Form(""),
    service: ConfigAdminService = Depends(get_config_admin_service),
) -> RedirectResponse:
    """Persist the monetization flag as a live override (Req 6.2)."""
    value = enabled.strip().lower() in {"1", "true", "on", "yes"}
    await service.set_monetization_enabled(value)
    response = _redirect()
    set_flash(
        response,
        f"Monetization {'enabled' if value else 'disabled'}.",
        "success",
    )
    return response


@router.post("/monetization/limits", dependencies=[Depends(require_csrf)])
async def set_limits(
    free_max: int = Form(...),
    free_modes: str = Form(""),
    paid_max: int = Form(...),
    paid_modes: str = Form(""),
    service: ConfigAdminService = Depends(get_config_admin_service),
) -> RedirectResponse:
    """Persist the per-tier plan limits as a live override (Req 6.3, 6.5)."""
    response = _redirect()
    if free_max < 1 or paid_max < 1:
        set_flash(
            response,
            "Maximum subscriptions must be at least 1 for every tier.",
            "error",
        )
        return response
    limits = {
        PLAN_TIER_FREE: PlanLimit(
            max_subscriptions=free_max, notification_modes=_parse_modes(free_modes)
        ),
        PLAN_TIER_PAID: PlanLimit(
            max_subscriptions=paid_max, notification_modes=_parse_modes(paid_modes)
        ),
    }
    try:
        await service.set_plan_limits(limits)
    except ConfigValidationError as exc:
        set_flash(response, str(exc), "error")
    else:
        set_flash(response, "Plan limits updated.", "success")
    return response
