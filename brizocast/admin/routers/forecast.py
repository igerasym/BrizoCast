"""Service Health page."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from brizocast.admin.dependencies import (
    get_admin_command_service,
    get_config_admin_service,
    get_forecast_cache_repository,
)
from brizocast.admin.flash import require_csrf, set_flash
from brizocast.admin.rendering import render
from brizocast.repositories.forecast_cache_repo import SqlAlchemyForecastCacheRepository
from brizocast.services.admin_command_service import AdminCommandService, AdminCommandType
from brizocast.services.config_admin_service import ConfigAdminService

router = APIRouter()

_REDIRECT = RedirectResponse(url="/forecast", status_code=status.HTTP_303_SEE_OTHER)

# Display name + description per tracker key.
_SERVICE_META: dict[str, tuple[str, str]] = {
    "forecast:open_meteo_marine": ("Open-Meteo Marine", "Wave & weather forecasts · free"),
    "forecast:stormglass":        ("Stormglass",         "Multi-model forecasts · 10 req/day"),
    "ai:gemini":                  ("Gemini AI",           "Regional preset generation"),
    "spotcatalog:surfline":       ("Surfline",            "Spot catalog ingestion"),
}


def _age(last_call: datetime | None) -> str:
    if last_call is None:
        return "never"
    delta = datetime.now(UTC) - last_call.astimezone(UTC)
    s = int(delta.total_seconds())
    if s < 60:    return "just now"
    if s < 3600:  return f"{s // 60} min ago"
    if s < 86400: return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _load_services(raw: object) -> list[dict[str, object]]:
    """Parse SERVICE_HEALTH snapshot into simple dicts for the template."""
    stored: dict[str, object] = raw if isinstance(raw, dict) else {}
    rows = []
    for key, (name, desc) in _SERVICE_META.items():
        entry = stored.get(key)
        if isinstance(entry, dict):
            last_call_dt = None
            if entry.get("last_call"):
                try:
                    last_call_dt = datetime.fromisoformat(str(entry["last_call"]))
                except (ValueError, TypeError):
                    pass
            stale = (
                last_call_dt is not None
                and datetime.now(UTC) - last_call_dt.astimezone(UTC) > timedelta(hours=24)
            )
            rows.append({
                "name": name,
                "desc": desc,
                "ok": bool(entry.get("ok")) and not stale,
                "message": str(entry.get("message", "")),
                "age": _age(last_call_dt),
                "ts": last_call_dt.strftime("%Y-%m-%d %H:%M UTC") if last_call_dt else None,
                "ms": f'{entry["response_ms"]:.0f} ms' if isinstance(entry.get("response_ms"), (int, float)) else None,
            })
        else:
            rows.append({"name": name, "desc": desc, "ok": None,
                         "message": "no calls yet", "age": "never", "ts": None, "ms": None})
    return rows


@router.get("/forecast", response_class=HTMLResponse)
async def forecast_page(
    request: Request,
    config: ConfigAdminService = Depends(get_config_admin_service),
) -> Response:
    raw = await config._store.get("SERVICE_HEALTH")
    return render(request, "forecast.html", {
        "services": _load_services(raw),
        "active_page": "forecast",
        "title": "Service Health",
    })


@router.post("/forecast/run-check", dependencies=[Depends(require_csrf)])
async def run_check(service: AdminCommandService = Depends(get_admin_command_service)) -> RedirectResponse:
    await service.enqueue(AdminCommandType.RUN_FORECAST_CHECK)
    r = RedirectResponse(url="/forecast", status_code=status.HTTP_303_SEE_OTHER)
    set_flash(r, "Forecast check enqueued.", "success")
    return r


@router.post("/forecast/cache/clear", dependencies=[Depends(require_csrf)])
async def clear_cache(cache: SqlAlchemyForecastCacheRepository = Depends(get_forecast_cache_repository)) -> RedirectResponse:
    removed = await cache.clear_all()
    r = RedirectResponse(url="/forecast", status_code=status.HTTP_303_SEE_OTHER)
    set_flash(r, f"Cleared {removed} cached forecast(s).", "success")
    return r
