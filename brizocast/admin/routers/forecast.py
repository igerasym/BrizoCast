"""Forecast management: provider settings, cache, run-now, service health."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from brizocast.admin.dependencies import (
    get_admin_command_service,
    get_config_admin_service,
    get_forecast_cache_repository,
    get_overrides,
)
from brizocast.admin.flash import require_csrf, set_flash
from brizocast.admin.rendering import render
from brizocast.config.overrides import OverrideAwareSettings
from brizocast.repositories.forecast_cache_repo import SqlAlchemyForecastCacheRepository
from brizocast.services.admin_command_service import AdminCommandService, AdminCommandType
from brizocast.services.config_admin_service import (
    ConfigAdminService,
    ConfigValidationError,
)

router = APIRouter()


def _redirect() -> RedirectResponse:
    return RedirectResponse(url="/forecast", status_code=status.HTTP_303_SEE_OTHER)


async def _gemini_status() -> dict[str, object]:
    """Check Gemini connectivity — returns {ok, model, message}."""
    from brizocast.admin.settings import load_panel_settings
    from pydantic import ValidationError
    try:
        panel = load_panel_settings()
    except SystemExit:
        return {"ok": False, "model": "unknown", "message": "Panel settings failed to load"}

    # Read AI settings from bot Settings (shared .env)
    try:
        from brizocast.config.settings import load_settings
        bot_settings = load_settings()
        ai_enabled = bot_settings.AI_ENABLED
        api_key = bot_settings.AI_API_KEY or ""
        model = bot_settings.AI_MODEL
    except Exception:
        ai_enabled = False
        api_key = ""
        model = "unknown"

    if not ai_enabled or not api_key:
        return {"ok": False, "model": model, "message": "Disabled (AI_ENABLED=false or no key)"}
    try:
        from google import genai as google_genai
        client = google_genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=model, contents="ping"
        )
        text_preview = (response.text or "")[:40].strip()
        return {"ok": True, "model": model, "message": f"OK — {text_preview!r}"}
    except Exception as exc:
        short = str(exc)[:120]
        return {"ok": False, "model": model, "message": short}


async def _surfline_status() -> dict[str, object]:
    """Check Surfline mapview reachability — returns {ok, message}."""
    import math
    import httpx
    try:
        url = "https://services.surfline.com/kbyg/mapview"
        params = {"south": 38.5, "west": -9.6, "north": 39.0, "east": -9.1}
        headers = {"User-Agent": "BrizoCast/0.1 (admin health check)"}
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url, params=params, headers=headers)
        if r.status_code == 200:
            data = r.json().get("data", {})
            spots = data.get("spots", [])
            return {"ok": True, "message": f"HTTP 200 — {len(spots)} spots returned"}
        return {"ok": False, "message": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)[:120]}


@router.get("/forecast", response_class=HTMLResponse)
async def forecast_page(
    request: Request,
    overrides: OverrideAwareSettings = Depends(get_overrides),
    config: ConfigAdminService = Depends(get_config_admin_service),
) -> Response:
    current = await overrides.forecast_provider()
    providers = await config.forecast_provider_overview(current)
    selectable = [p for p in providers if p.enabled]

    gemini = await _gemini_status()
    surfline = await _surfline_status()

    return render(
        request,
        "forecast.html",
        {
            "current": current,
            "providers": providers,
            "selectable": selectable,
            "gemini": gemini,
            "surfline": surfline,
            "active_page": "forecast",
            "title": "Forecast",
        },
    )


@router.post("/forecast/run-check", dependencies=[Depends(require_csrf)])
async def run_check(
    service: AdminCommandService = Depends(get_admin_command_service),
) -> RedirectResponse:
    """Enqueue a run-forecast-check command."""
    await service.enqueue(AdminCommandType.RUN_FORECAST_CHECK)
    response = _redirect()
    set_flash(response, "Forecast check enqueued — bot will run it shortly.", "success")
    return response


@router.post("/forecast/providers", dependencies=[Depends(require_csrf)])
async def set_enabled_providers(
    request: Request,
    overrides: OverrideAwareSettings = Depends(get_overrides),
    config: ConfigAdminService = Depends(get_config_admin_service),
) -> RedirectResponse:
    form = await request.form()
    selected = [str(value) for value in form.getlist("enabled")]
    active = await overrides.forecast_provider()
    response = _redirect()
    try:
        await config.set_forecast_providers_enabled(selected, active_provider=active)
    except ConfigValidationError as exc:
        set_flash(response, str(exc), "error")
    else:
        set_flash(response, "Forecast providers updated.", "success")
    return response


@router.post("/forecast/provider", dependencies=[Depends(require_csrf)])
async def set_provider(
    provider: str = Form(...),
    config: ConfigAdminService = Depends(get_config_admin_service),
) -> RedirectResponse:
    response = _redirect()
    try:
        await config.set_forecast_provider(provider)
    except ConfigValidationError as exc:
        set_flash(response, str(exc), "error")
    else:
        set_flash(response, f"Active provider set to {provider!r}.", "success")
    return response


@router.post("/forecast/cache/clear", dependencies=[Depends(require_csrf)])
async def clear_cache(
    cache: SqlAlchemyForecastCacheRepository = Depends(get_forecast_cache_repository),
) -> RedirectResponse:
    removed = await cache.clear_all()
    response = _redirect()
    set_flash(response, f"Cleared {removed} cached forecast(s).", "success")
    return response
