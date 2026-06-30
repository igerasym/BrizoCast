"""Dashboard and stats/health routes (Req 11.1, 11.2, 11.3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from brizocast.admin import queries
from brizocast.admin.dependencies import get_scheduler_state, get_spot_repository
from brizocast.admin.queries import StatsView
from brizocast.admin.rendering import render
from brizocast.repositories.json_spot_repo import JsonSpotRepository
from brizocast.services.sqlite_scheduler_state import SqliteSchedulerState

router = APIRouter()


async def _build_stats(
    request: Request,
    spots: JsonSpotRepository,
    scheduler_state: SqliteSchedulerState,
) -> StatsView:
    """Assemble the dashboard stats from the DB, spot dataset, and scheduler row."""
    session_factory = request.app.state.container.session_factory
    total_users, counts, total_subs = await queries.tier_counts(session_factory)
    try:
        total_spots = len(spots.all_spots())
    except Exception:  # noqa: BLE001 - a missing/!malformed dataset shouldn't 500 the page.
        total_spots = 0
    last_run = await scheduler_state.last_successful_run_async()
    return StatsView(
        total_users=total_users,
        tier_counts=counts,
        total_subscriptions=total_subs,
        total_spots=total_spots,
        last_scheduler_run=last_run,
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    spots: JsonSpotRepository = Depends(get_spot_repository),
    scheduler_state: SqliteSchedulerState = Depends(get_scheduler_state),
) -> Response:
    """Render the dashboard (same stats as ``/stats``) (Req 11.*)."""
    stats = await _build_stats(request, spots, scheduler_state)
    return render(
        request,
        "stats.html",
        {"stats": stats, "active_page": "dashboard", "title": "Dashboard"},
    )


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(
    request: Request,
    spots: JsonSpotRepository = Depends(get_spot_repository),
    scheduler_state: SqliteSchedulerState = Depends(get_scheduler_state),
) -> Response:
    """Render the stats/health page (Req 11.1, 11.2, 11.3)."""
    stats = await _build_stats(request, spots, scheduler_state)
    return render(
        request,
        "stats.html",
        {"stats": stats, "active_page": "stats", "title": "Stats"},
    )
