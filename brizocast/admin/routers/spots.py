"""Surf-spot CRUD routes over the shared JSON dataset (Req 4.*)."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from brizocast.admin.flash import require_csrf, set_flash
from brizocast.admin.rendering import render
from brizocast.admin.settings import PanelSettings
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.errors import NotFoundError
from brizocast.services.spot_admin_service import SpotAdminService, SpotValidationError

router = APIRouter()


def get_spot_admin(request: Request) -> SpotAdminService:
    """Build the surf-spot admin service over the shared dataset path."""
    panel: PanelSettings = request.app.state.panel
    return SpotAdminService(panel.SPOT_DATASET_PATH)


def _redirect() -> RedirectResponse:
    """Return a post-action redirect back to the spots list."""
    return RedirectResponse(url="/spots", status_code=status.HTTP_303_SEE_OTHER)


def _contains(value: str | None, term: str) -> bool:
    """Case-insensitive substring match (an empty term matches everything)."""
    if not term:
        return True
    return term.casefold() in (value or "").casefold()


def _filter_and_sort(
    spots: Iterable[SurfSpot],
    *,
    country: str,
    region: str,
    name: str,
    key: str,
) -> list[SurfSpot]:
    """Filter spots by per-column substrings and sort by country/region/name."""
    matched = [
        spot
        for spot in spots
        if _contains(spot.country, country)
        and _contains(spot.region, region)
        and _contains(spot.name, name)
        and _contains(spot.spot_key, key)
    ]
    return sorted(
        matched,
        key=lambda s: (
            (s.country or "").casefold(),
            (s.region or "").casefold(),
            s.name.casefold(),
        ),
    )


_PAGE_SIZE = 50


@router.get("/spots", response_class=HTMLResponse)
async def list_spots(
    request: Request,
    country: str = "",
    region: str = "",
    name: str = "",
    key: str = "",
    page: int = 1,
    service: SpotAdminService = Depends(get_spot_admin),
) -> Response:
    """List the surf-spot catalogue with per-column filters and pagination."""
    all_spots = _filter_and_sort(
        await service.list_spots(),
        country=country,
        region=region,
        name=name,
        key=key,
    )
    total = len(all_spots)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * _PAGE_SIZE
    spots = all_spots[offset: offset + _PAGE_SIZE]

    return render(
        request,
        "spots_list.html",
        {
            "spots": spots,
            "filters": {
                "country": country,
                "region": region,
                "name": name,
                "key": key,
            },
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "active_page": "spots",
            "title": "Surf spots",
        },
    )


@router.get("/spots/table", response_class=HTMLResponse)
async def spots_table(
    request: Request,
    country: str = "",
    region: str = "",
    name: str = "",
    key: str = "",
    page: int = 1,
    service: SpotAdminService = Depends(get_spot_admin),
) -> Response:
    """Return just the filtered spots table for live HTMX swaps."""
    all_spots = _filter_and_sort(
        await service.list_spots(),
        country=country,
        region=region,
        name=name,
        key=key,
    )
    total = len(all_spots)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * _PAGE_SIZE
    spots = all_spots[offset: offset + _PAGE_SIZE]

    templates = request.app.state.templates
    response: Response = templates.TemplateResponse(
        request,
        "_partials/_spots_table.html",
        {"spots": spots, "page": page, "total_pages": total_pages, "total": total},
    )
    return response


@router.post("/spots", dependencies=[Depends(require_csrf)])
async def create_spot(
    spot_key: str = Form(...),
    name: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    country: str = Form(""),
    region: str = Form(""),
    service: SpotAdminService = Depends(get_spot_admin),
) -> RedirectResponse:
    """Create a surf spot, validating coordinates and unique id (Req 4.2, 4.5, 4.6)."""
    response = _redirect()
    try:
        await service.create_spot(
            spot_key=spot_key,
            name=name,
            lat=lat,
            lon=lon,
            country=country or None,
            region=region or None,
        )
    except SpotValidationError as exc:
        set_flash(response, str(exc), "error")
    else:
        set_flash(response, f"Created surf spot {spot_key!r}.", "success")
    return response


@router.post("/spots/{spot_key}", dependencies=[Depends(require_csrf)])
async def edit_spot(
    spot_key: str,
    name: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    country: str = Form(""),
    region: str = Form(""),
    service: SpotAdminService = Depends(get_spot_admin),
) -> RedirectResponse:
    """Edit an existing surf spot's fields (Req 4.3, 4.5)."""
    response = _redirect()
    try:
        await service.edit_spot(
            spot_key,
            name=name,
            lat=lat,
            lon=lon,
            country=country or None,
            region=region or None,
        )
    except SpotValidationError as exc:
        set_flash(response, str(exc), "error")
    except NotFoundError as exc:
        set_flash(response, str(exc), "error")
    else:
        set_flash(response, f"Updated surf spot {spot_key!r}.", "success")
    return response


@router.post("/spots/{spot_key}/delete", dependencies=[Depends(require_csrf)])
async def delete_spot(
    spot_key: str,
    service: SpotAdminService = Depends(get_spot_admin),
) -> RedirectResponse:
    """Delete a surf spot from the dataset (Req 4.4).

    Modelled as a POST (rather than DELETE) so a plain HTML form can trigger it.
    """
    response = _redirect()
    try:
        await service.delete_spot(spot_key)
    except NotFoundError as exc:
        set_flash(response, str(exc), "error")
    else:
        set_flash(response, f"Deleted surf spot {spot_key!r}.", "success")
    return response
