"""Spot ingestion — grow our own spot dataset from a catalogue source.

When a user provides a location (shared coordinates or a searched city), the bot
asks a :class:`~brizocast.providers.spotcatalog.base.SpotCatalogProvider`
(Surfline) for the named surf spots in that area and merges the new ones into
the shared surf-spot dataset via
:class:`~brizocast.services.spot_admin_service.SpotAdminService`. From then on
the bot serves discovery and forecasts from **our** dataset (forecasts come from
Open-Meteo by coordinate); the catalogue is only the source of *names +
coordinates*.

Design choices:
* **Graceful** — a catalogue failure (e.g. Surfline bot protection / HTTP 403,
  or a network error) never propagates: ingestion logs it and reports zero new
  spots so the user-facing flow continues from the existing dataset.
* **Polite / cached** — each ~0.5° area cell is imported at most once per TTL,
  so repeated shares from the same region do not re-hit the catalogue.
* **Deduplicated** — a candidate is skipped when its key already exists or when
  an existing spot lies within a small distance (default 500 m), so overlapping
  radii and pre-seeded spots do not create near-duplicates.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from brizocast.core.domain.spot import SurfSpot
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.providers.geocoding.reverse import NominatimReverseGeocoder
from brizocast.providers.spotcatalog.base import SpotCatalogError, SpotCatalogProvider
from brizocast.services.spot_admin_service import SpotAdminService, SpotValidationError

if TYPE_CHECKING:
    from brizocast.services.preset_service import PresetService

__all__ = ["IngestResult", "SpotIngestionService"]

_DEDUP_KM: Final = 0.5
_CELL_TTL_SECONDS: Final = 24 * 60 * 60
_NAME_RE: Final = re.compile(r"[^a-z0-9]+")
_EARTH_RADIUS_KM: Final = 6371.0


def _normalize(name: str) -> str:
    """Normalise a spot name for fuzzy duplicate comparison."""
    return _NAME_RE.sub("", name.casefold())


def _degrees_to_compass(deg: float | None) -> str | None:
    """Convert degrees to nearest 16-point compass string, or None."""
    if deg is None:
        return None
    try:
        from brizocast.activities.surf.directions import degrees_to_compass
        return degrees_to_compass(deg)
    except Exception:  # noqa: BLE001
        return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two coordinates."""
    p = math.radians
    dlat = p(lat2 - lat1)
    dlon = p(lon2 - lon1)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(p(lat1)) * math.cos(p(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of an :meth:`SpotIngestionService.ingest_near` call.

    Attributes:
        found: Spots the catalogue returned for the area.
        added: New spots written to the dataset.
        skipped: Candidates skipped as duplicates or invalid.
        from_cache: ``True`` when the area was recently imported and no
            catalogue call was made.
    """

    found: int = 0
    added: int = 0
    skipped: int = 0
    from_cache: bool = False


class SpotIngestionService:
    """Imports catalogue spots for an area into the shared dataset."""

    def __init__(
        self,
        catalog: SpotCatalogProvider,
        spot_admin: SpotAdminService,
        *,
        reverse_geocoder: NominatimReverseGeocoder | None = None,
        preset_service: "PresetService | None" = None,
        dedup_km: float = _DEDUP_KM,
        cell_ttl_seconds: float = _CELL_TTL_SECONDS,
        logger: BoundLogger | None = None,
    ) -> None:
        self._catalog = catalog
        self._spot_admin = spot_admin
        self._reverse_geocoder = reverse_geocoder
        self._preset_service = preset_service
        self._dedup_km = dedup_km
        self._cell_ttl = cell_ttl_seconds
        self._log = logger or get_logger(__name__)
        self._imported_cells: dict[tuple[int, int], float] = {}

    async def ingest_near(
        self, lat: float, lon: float, radius_km: float
    ) -> IngestResult:
        """Import catalogue spots near ``(lat, lon)`` into the dataset (graceful).

        Returns an :class:`IngestResult`. A catalogue error is swallowed (logged)
        and reported as zero new spots so the caller's flow is never blocked.
        """
        cell = self._cell_for(lat, lon)
        if self._cell_is_fresh(cell):
            return IngestResult(from_cache=True)

        try:
            candidates = await self._catalog.spots_near(lat, lon, radius_km)
        except SpotCatalogError as exc:
            self._log.warning(
                "spot catalogue (%s) unavailable for (%.4f, %.4f): %s; "
                "serving existing dataset",
                self._catalog.key,
                lat,
                lon,
                exc,
            )
            return IngestResult()

        # Mark the cell imported even on an empty result, so we do not re-hit the
        # catalogue for a sparse area within the TTL.
        self._imported_cells[cell] = time.monotonic()

        existing = await self._spot_admin.list_spots()
        added = 0
        skipped = 0
        for candidate in candidates:
            if self._is_duplicate(candidate, existing):
                skipped += 1
                continue
            candidate = await self._enrich(candidate)
            try:
                await self._spot_admin.create_spot(
                    candidate.spot_key,
                    candidate.name,
                    candidate.lat,
                    candidate.lon,
                    candidate.country,
                    candidate.region,
                )
            except SpotValidationError:
                skipped += 1
                continue
            existing.append(candidate)  # dedup subsequent candidates against it
            added += 1

        self._log.info(
            "ingested %d new spot(s) from %s near (%.4f, %.4f); %d skipped",
            added,
            self._catalog.key,
            lat,
            lon,
            skipped,
        )

        # Auto-generate AI regional presets for new regions that don't have one yet.
        if added > 0 and self._preset_service is not None:
            await self._bootstrap_region_presets(existing)

        return IngestResult(found=len(candidates), added=added, skipped=skipped)

    async def ensure_region_presets(self, lat: float, lon: float) -> None:
        """Ensure AI presets exist for regions near (lat, lon), regardless of cache.

        Used when subscribing to an already-ingested location so presets are
        always bootstrapped even if the cell cache is fresh.
        """
        if self._preset_service is None:
            return
        existing = await self._spot_admin.list_spots()
        if existing:
            await self._bootstrap_region_presets(existing)

    # -- internals ------------------------------------------------------- #

    async def _bootstrap_region_presets(self, spots: list[SurfSpot]) -> None:
        """Generate and persist AI presets for new regions (graceful)."""
        if self._preset_service is None:
            return
        regions = {s.region for s in spots if s.region}
        if not regions:
            return
        for region in regions:
            try:
                from brizocast.services.preset_service import PresetSource
                options = await self._preset_service.list_presets(0, region=region)
                has_persisted = any(
                    o.source == PresetSource.PERSISTED_DEFAULT for o in options
                )
                if has_persisted:
                    continue

                # Call AI provider directly for accurate region-specific params.
                ai_provider = getattr(self._preset_service, "_ai_provider", None)
                if ai_provider is None or not ai_provider.is_available():
                    self._log.debug("AI provider unavailable; skipping preset for %r", region)
                    continue

                params = await ai_provider.generate_region_preset(region, "surf")

                from brizocast.services.preset_admin_service import PresetAdminService
                admin = PresetAdminService(
                    self._preset_service._session_factory,
                    logger=self._log,
                )
                await admin.create_regional_preset(
                    region=region,
                    name=f"{region} — AI Generated",
                    min_wave_m=params.min_wave_m,
                    max_wave_m=params.max_wave_m,
                    min_period_s=params.min_period_s,
                    max_wind_kmh=params.max_wind_kmh,
                    preferred_wind_dir=_degrees_to_compass(params.preferred_wind_dir_deg),
                    preferred_swell_dir=_degrees_to_compass(params.preferred_swell_dir_deg),
                    min_alert_score=params.min_alert_score,
                )
                self._log.info(
                    "auto-generated AI preset for new region %r "
                    "(wave %.1f-%.1fm, period %.0fs, wind %.0f km/h)",
                    region,
                    params.min_wave_m,
                    params.max_wave_m,
                    params.min_period_s,
                    params.max_wind_kmh,
                )
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "failed to bootstrap preset for region %r: %s", region, exc
                )

    async def _enrich(self, candidate: SurfSpot) -> SurfSpot:
        """Fill in a candidate's missing ``country``/``region`` via reverse geocoding.

        Returns ``candidate`` unchanged when no geocoder is configured or when
        both fields are already present; otherwise returns a copy with whatever
        the geocoder resolved (geocoding is graceful and may still yield
        ``None`` for either field).
        """
        if self._reverse_geocoder is None:
            return candidate
        if candidate.country and candidate.region:
            return candidate
        country, region = await self._reverse_geocoder.reverse(
            candidate.lat, candidate.lon
        )
        updates: dict[str, str] = {}
        if not candidate.country and country:
            updates["country"] = country
        if not candidate.region and region:
            updates["region"] = region
        if not updates:
            return candidate
        return candidate.model_copy(update=updates)

    def _is_duplicate(self, candidate: SurfSpot, existing: list[SurfSpot]) -> bool:
        """Whether ``candidate`` duplicates an existing spot (key or proximity)."""
        cand_name = _normalize(candidate.name)
        for spot in existing:
            if spot.spot_key == candidate.spot_key:
                return True
            distance = _haversine_km(
                candidate.lat, candidate.lon, spot.lat, spot.lon
            )
            if distance <= self._dedup_km and _normalize(spot.name) == cand_name:
                return True
        return False

    @staticmethod
    def _cell_for(lat: float, lon: float) -> tuple[int, int]:
        """Return the ~0.5° area-cell key containing ``(lat, lon)``."""
        return (round(lat * 2), round(lon * 2))

    def _cell_is_fresh(self, cell: tuple[int, int]) -> bool:
        """Whether ``cell`` was imported within the TTL (skip a re-query)."""
        imported_at = self._imported_cells.get(cell)
        if imported_at is None:
            return False
        return (time.monotonic() - imported_at) < self._cell_ttl
