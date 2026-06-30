"""JSON-backed :class:`SpotRepository` implementation (Req 5.1, 5.2, 5.6).

``JsonSpotRepository`` loads the bundled ``storage/spots/surf_spots.json`` seed
dataset, validates each entry into the pure domain
:class:`~brizocast.core.domain.spot.SurfSpot` value object, caches the parsed
spots in memory, and serves radius-bounded discovery by delegating to the pure
:func:`~brizocast.core.domain.geo.spots_within` helper.

It conforms to the :class:`~brizocast.core.ports.spot_repository.SpotRepository`
port structurally, so the JSON backing can later be swapped for a database
without changing the discovery logic that depends only on the port (Req 5.6).

An optional ``dataset_path`` is accepted for testability; when omitted the
repository reads the JSON shipped inside the package via :mod:`importlib.resources`
so it resolves correctly whether running from source or an installed wheel.
"""

from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from brizocast.core.domain.geo import GeoPoint, spots_within
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.errors import BrizoCastError
from brizocast.core.logging import BoundLogger, get_logger

__all__ = ["JsonSpotRepository", "SpotDatasetError", "ensure_spot_dataset_seeded"]

# Package and resource name of the bundled seed dataset. Declared in
# pyproject's ``[tool.setuptools.package-data]`` so it ships with the wheel.
_DATASET_PACKAGE: Final[str] = "brizocast.storage.spots"
_DATASET_RESOURCE: Final[str] = "surf_spots.json"


class SpotDatasetError(BrizoCastError):
    """Raised when the surf-spot dataset is missing or malformed.

    A bundled/deployed dataset that cannot be read or parsed is a deployment
    integrity failure (not a recoverable user error), so it surfaces as a loud
    domain error at first access rather than silently yielding no spots.
    """


class JsonSpotRepository:
    """Serves surf spots from a JSON dataset, cached in memory.

    The dataset is loaded and validated lazily on first access and then cached
    for the lifetime of the instance, so repeated discovery calls during a
    scheduler run incur no re-parsing.
    """

    def __init__(
        self,
        dataset_path: str | Path | None = None,
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the repository.

        Args:
            dataset_path: Optional filesystem path to a JSON dataset. When
                ``None`` (the default), the bundled package dataset is used.
            logger: Optional bound logger; one is created when omitted.
        """
        self._dataset_path = Path(dataset_path) if dataset_path is not None else None
        self._log = logger or get_logger(__name__)
        self._spots: tuple[SurfSpot, ...] | None = None
        # mtime (ns) of the dataset file at the time ``_spots`` was last loaded;
        # used to detect out-of-process edits (e.g. by the admin panel) so the
        # bot picks up surf-spot changes without a restart (Req 14.2).
        self._loaded_mtime_ns: int | None = None

    # -- loading --------------------------------------------------------- #

    def _read_text(self) -> str:
        """Return the raw JSON text from the configured path or bundled resource."""
        if self._dataset_path is not None:
            try:
                return self._dataset_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise SpotDatasetError(
                    f"could not read surf-spot dataset at {self._dataset_path}: {exc}"
                ) from exc
        try:
            resource = resources.files(_DATASET_PACKAGE).joinpath(_DATASET_RESOURCE)
            return resource.read_text(encoding="utf-8")
        except (OSError, ModuleNotFoundError) as exc:
            raise SpotDatasetError(
                f"could not read bundled surf-spot dataset {_DATASET_PACKAGE}/{_DATASET_RESOURCE}: {exc}"
            ) from exc

    def _parse(self, text: str) -> tuple[SurfSpot, ...]:
        """Parse and validate the dataset text into ``SurfSpot`` objects."""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpotDatasetError(f"surf-spot dataset is not valid JSON: {exc}") from exc

        if not isinstance(payload, list):
            raise SpotDatasetError(
                f"surf-spot dataset must be a JSON array, got {type(payload).__name__}"
            )

        spots: list[SurfSpot] = []
        seen: set[str] = set()
        for index, entry in enumerate(payload):
            try:
                spot = SurfSpot.model_validate(entry)
            except ValidationError as exc:
                raise SpotDatasetError(
                    f"invalid surf-spot entry at index {index}: {exc}"
                ) from exc
            if spot.spot_key in seen:
                raise SpotDatasetError(
                    f"duplicate spot_key {spot.spot_key!r} at index {index}"
                )
            seen.add(spot.spot_key)
            spots.append(spot)
        return tuple(spots)

    def _load(self) -> tuple[SurfSpot, ...]:
        """Load, validate, and cache the dataset, reloading on file changes.

        When the repository is backed by a filesystem path (the shared
        ``./data`` dataset), the file's modification time is checked on every
        access: if it changed since the cached read — for instance after the
        admin panel wrote an edit — the dataset is reparsed so the bot serves
        the fresh spots without a restart (Req 14.2). The bundled package
        resource never changes at runtime, so it is parsed once.
        """
        if self._dataset_path is not None:
            current_mtime = self._current_mtime_ns()
            if self._spots is None or current_mtime != self._loaded_mtime_ns:
                spots = self._parse(self._read_text())
                self._spots = spots
                self._loaded_mtime_ns = current_mtime
                self._log.info(
                    "loaded %d surf spots from %s", len(spots), self._dataset_path
                )
            return self._spots

        if self._spots is None:
            spots = self._parse(self._read_text())
            self._spots = spots
            self._log.info("loaded %d surf spots from bundled dataset", len(spots))
        return self._spots

    def _current_mtime_ns(self) -> int | None:
        """Return the dataset file's mtime in ns, or ``None`` if it is absent."""
        assert self._dataset_path is not None  # noqa: S101 - guarded by caller
        try:
            return self._dataset_path.stat().st_mtime_ns
        except OSError:
            return None

    # -- SpotRepository port -------------------------------------------- #

    def all_spots(self) -> list[SurfSpot]:
        """Return every known surf spot (Req 5.1)."""
        return list(self._load())

    def spots_within(self, center: GeoPoint, radius_km: float) -> list[SurfSpot]:
        """Return the spots within ``radius_km`` of ``center`` (inclusive) (Req 5.3)."""
        return spots_within(center, radius_km, self._load())


def _bundled_dataset_text() -> str:
    """Return the raw JSON text of the bundled seed dataset.

    Raises:
        SpotDatasetError: If the packaged resource cannot be read.
    """
    try:
        resource = resources.files(_DATASET_PACKAGE).joinpath(_DATASET_RESOURCE)
        return resource.read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError) as exc:
        raise SpotDatasetError(
            f"could not read bundled surf-spot dataset "
            f"{_DATASET_PACKAGE}/{_DATASET_RESOURCE}: {exc}"
        ) from exc


async def ensure_spot_dataset_seeded(path: str | Path) -> None:
    """Copy the bundled surf-spot dataset to ``path`` once, if it is absent.

    Both the bot and the admin panel point their :class:`JsonSpotRepository` at
    the shared ``data/surf_spots.json`` file so the panel can edit spots and the
    bot can read them (Req 4.1, 14.2). On first startup that file does not exist
    yet, so this seeds it from the read-only bundled package resource. The write
    is atomic (temp file in the same directory + :func:`os.replace`) so a reader
    never sees a half-written file. If the target already exists this is a no-op,
    preserving any edits the panel has made.

    Args:
        path: Filesystem path of the shared dataset (e.g. ``data/surf_spots.json``).
    """
    target = Path(path)
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    text = _bundled_dataset_text()
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
