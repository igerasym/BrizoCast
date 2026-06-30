"""Surf-spot dataset admin service (Req 4.2-4.6).

``SpotAdminService`` is the panel-side use case for editing the shared surf-spot
dataset — the ``data/surf_spots.json`` file on the ``./data`` volume that the
bot's :class:`~brizocast.repositories.json_spot_repo.JsonSpotRepository` reads.
It supports create, edit, and delete with validation, and is the *only* writer
of that file.

Atomic writes
-------------
The bot reads the same file (reloading on mtime change), so a write must never
expose a half-written file. Every mutation rewrites the whole dataset to a
temporary file in the same directory and then :func:`os.replace`-s it over the
target — an atomic rename on the same filesystem, so a concurrent reader sees
either the complete old file or the complete new one, never a torn one. A
coarse in-process :class:`asyncio.Lock` serialises the panel's own writes.

Validation (each rejects *without* mutating the file)
-----------------------------------------------------
* latitude within ``[-90, 90]`` and longitude within ``[-180, 180]`` (Req 4.5);
* a unique ``spot_key`` on create (Req 4.6).

Requirements covered: 4.2, 4.3, 4.4, 4.5, 4.6.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from pydantic import ValidationError

from brizocast.core.domain.geo import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.errors import NotFoundError
from brizocast.core.logging import BoundLogger, get_logger

__all__ = ["SpotAdminService", "SpotValidationError"]


class SpotValidationError(ValueError):
    """Raised when a surf-spot submission is invalid (Req 4.5, 4.6).

    Subclasses :class:`ValueError` so the spots router can map it to an HTTP 400
    with the specific message while leaving the dataset file unchanged.
    """


class SpotAdminService:
    """Create, edit, and delete surf spots in the shared JSON dataset (Req 4.*)."""

    def __init__(
        self,
        dataset_path: str | Path,
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            dataset_path: Filesystem path of the shared ``surf_spots.json``.
            logger: Optional bound logger; one is created when omitted.
        """
        self._path = Path(dataset_path)
        self._lock = asyncio.Lock()
        self._log = logger or get_logger(__name__)

    # -- reads ----------------------------------------------------------- #

    async def list_spots(self) -> list[SurfSpot]:
        """Return every surf spot in the dataset, in file order (Req 4.1)."""
        return self._read()

    # -- writes ---------------------------------------------------------- #

    async def create_spot(
        self,
        spot_key: str,
        name: str,
        lat: float,
        lon: float,
        country: str | None = None,
        region: str | None = None,
    ) -> SurfSpot:
        """Create and persist a new surf spot (Req 4.2).

        Raises:
            SpotValidationError: If the coordinates are out of range (Req 4.5)
                or ``spot_key`` already exists (Req 4.6). The file is unchanged.
        """
        spot = self._validated_spot(spot_key, name, lat, lon, country, region)
        async with self._lock:
            spots = self._read()
            if any(existing.spot_key == spot.spot_key for existing in spots):
                raise SpotValidationError(
                    f"a surf spot with identifier {spot.spot_key!r} already exists"
                )
            spots.append(spot)
            self._write(spots)
            self._log.info("created surf spot %r", spot.spot_key)
            return spot

    async def edit_spot(
        self,
        spot_key: str,
        *,
        name: str,
        lat: float,
        lon: float,
        country: str | None = None,
        region: str | None = None,
    ) -> SurfSpot:
        """Edit an existing surf spot's fields and persist them (Req 4.3).

        The ``spot_key`` identifies the spot to edit and is preserved.

        Raises:
            SpotValidationError: If the coordinates are out of range (Req 4.5).
            NotFoundError: If no spot with ``spot_key`` exists.
        """
        updated = self._validated_spot(spot_key, name, lat, lon, country, region)
        async with self._lock:
            spots = self._read()
            index = self._index_of(spots, spot_key)
            if index is None:
                raise NotFoundError(f"surf spot {spot_key!r} does not exist")
            spots[index] = updated
            self._write(spots)
            self._log.info("edited surf spot %r", spot_key)
            return updated

    async def delete_spot(self, spot_key: str) -> None:
        """Remove the surf spot with ``spot_key`` from the dataset (Req 4.4).

        Raises:
            NotFoundError: If no spot with ``spot_key`` exists.
        """
        async with self._lock:
            spots = self._read()
            index = self._index_of(spots, spot_key)
            if index is None:
                raise NotFoundError(f"surf spot {spot_key!r} does not exist")
            del spots[index]
            self._write(spots)
            self._log.info("deleted surf spot %r", spot_key)

    # -- internals ------------------------------------------------------- #

    @staticmethod
    def _index_of(spots: list[SurfSpot], spot_key: str) -> int | None:
        """Return the index of ``spot_key`` in ``spots``, or ``None`` if absent."""
        for index, spot in enumerate(spots):
            if spot.spot_key == spot_key:
                return index
        return None

    @staticmethod
    def _validated_spot(
        spot_key: str,
        name: str,
        lat: float,
        lon: float,
        country: str | None,
        region: str | None,
    ) -> SurfSpot:
        """Build a validated :class:`SurfSpot`, raising on invalid input (Req 4.5).

        Coordinates are bounds-checked explicitly first so the error names the
        offending coordinate; the :class:`SurfSpot` model then enforces the same
        bounds and the non-empty key/name.
        """
        if not LAT_MIN <= lat <= LAT_MAX:
            raise SpotValidationError(
                f"latitude must be between {LAT_MIN:g} and {LAT_MAX:g} "
                f"(got {lat:g})"
            )
        if not LON_MIN <= lon <= LON_MAX:
            raise SpotValidationError(
                f"longitude must be between {LON_MIN:g} and {LON_MAX:g} "
                f"(got {lon:g})"
            )
        try:
            return SurfSpot(
                spot_key=spot_key,
                name=name,
                lat=lat,
                lon=lon,
                country=country or None,
                region=region or None,
            )
        except ValidationError as exc:
            raise SpotValidationError(str(exc)) from exc

    def _read(self) -> list[SurfSpot]:
        """Read and parse the dataset file into a list of :class:`SurfSpot`.

        Returns an empty list when the file does not exist yet (the panel can
        seed the first spot). Raises :class:`SpotValidationError` if the file is
        present but not a valid JSON array of spots.
        """
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpotValidationError(
                f"surf-spot dataset is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, list):
            raise SpotValidationError("surf-spot dataset must be a JSON array")
        spots: list[SurfSpot] = []
        for index, entry in enumerate(payload):
            try:
                spots.append(SurfSpot.model_validate(entry))
            except ValidationError as exc:
                raise SpotValidationError(
                    f"invalid surf-spot entry at index {index}: {exc}"
                ) from exc
        return spots

    def _write(self, spots: list[SurfSpot]) -> None:
        """Atomically replace the dataset file with ``spots`` (temp + replace)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [spot.model_dump(mode="json") for spot in spots]
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._path)
