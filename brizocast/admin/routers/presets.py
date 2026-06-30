"""Regional default-preset routes (DB-backed)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from brizocast.admin.dependencies import get_preset_admin_service
from brizocast.admin.flash import require_csrf, set_flash
from brizocast.admin.rendering import render
from brizocast.core.errors import NotFoundError
from brizocast.services.preset_admin_service import (
    PresetAdminService,
    PresetValidationError,
)

router = APIRouter()


def _redirect() -> RedirectResponse:
    return RedirectResponse(url="/presets", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/presets", response_class=HTMLResponse)
async def list_presets(
    request: Request,
    service: PresetAdminService = Depends(get_preset_admin_service),
) -> Response:
    presets = await service.list_regional_presets()
    return render(
        request,
        "presets_list.html",
        {"presets": presets, "active_page": "presets", "title": "Regional presets"},
    )


@router.post("/presets", dependencies=[Depends(require_csrf)])
async def create_preset(
    region: str = Form(...),
    name: str = Form(...),
    min_wave_m: float = Form(...),
    max_wave_m: float = Form(...),
    min_period_s: float = Form(...),
    max_wind_kmh: float = Form(...),
    preferred_wind_dir: str = Form(""),
    preferred_swell_dir: str = Form(""),
    min_alert_score: Optional[int] = Form(None),
    service: PresetAdminService = Depends(get_preset_admin_service),
) -> RedirectResponse:
    response = _redirect()
    try:
        await service.create_regional_preset(
            region=region,
            name=name,
            min_wave_m=min_wave_m,
            max_wave_m=max_wave_m,
            min_period_s=min_period_s,
            max_wind_kmh=max_wind_kmh,
            preferred_wind_dir=preferred_wind_dir or None,
            preferred_swell_dir=preferred_swell_dir or None,
            min_alert_score=min_alert_score,
        )
    except PresetValidationError as exc:
        set_flash(response, str(exc), "error")
    else:
        set_flash(response, f"Created preset {name!r} for {region}.", "success")
    return response


@router.post("/presets/{preset_id}", dependencies=[Depends(require_csrf)])
async def edit_preset(
    preset_id: int,
    region: str = Form(...),
    name: str = Form(...),
    min_wave_m: float = Form(...),
    max_wave_m: float = Form(...),
    min_period_s: float = Form(...),
    max_wind_kmh: float = Form(...),
    preferred_wind_dir: str = Form(""),
    preferred_swell_dir: str = Form(""),
    min_alert_score: Optional[int] = Form(None),
    service: PresetAdminService = Depends(get_preset_admin_service),
) -> RedirectResponse:
    response = _redirect()
    try:
        await service.edit_regional_preset(
            preset_id,
            region=region,
            name=name,
            min_wave_m=min_wave_m,
            max_wave_m=max_wave_m,
            min_period_s=min_period_s,
            max_wind_kmh=max_wind_kmh,
            preferred_wind_dir=preferred_wind_dir or None,
            preferred_swell_dir=preferred_swell_dir or None,
            min_alert_score=min_alert_score,
        )
    except PresetValidationError as exc:
        set_flash(response, str(exc), "error")
    except NotFoundError as exc:
        set_flash(response, str(exc), "error")
    else:
        set_flash(response, f"Updated preset {preset_id}.", "success")
    return response
