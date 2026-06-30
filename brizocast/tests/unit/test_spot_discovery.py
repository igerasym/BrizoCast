"""Unit tests for the JSON spot repository and discovery service (task 3.6).

Covers loading and caching the bundled seed dataset, parsing into domain
``SurfSpot`` objects, radius discovery returning the expected nearby spots, the
malformed-dataset failure path, and the empty-discovery skip signal that lets
the scheduler skip forecast collection (Req 5.1, 5.2, 5.3, 5.5, 5.6).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brizocast.core.domain.geo import GeoPoint
from brizocast.core.domain.spot import SurfSpot
from brizocast.repositories.json_spot_repo import JsonSpotRepository, SpotDatasetError
from brizocast.services.spot_discovery_service import (
    SpotDiscoveryResult,
    SpotDiscoveryService,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# JsonSpotRepository — bundled dataset
# --------------------------------------------------------------------------- #
def test_bundled_dataset_loads_surf_spots() -> None:
    repo = JsonSpotRepository()
    spots = repo.all_spots()
    assert len(spots) >= 15
    assert all(isinstance(spot, SurfSpot) for spot in spots)


def test_bundled_dataset_has_unique_keys_and_required_fields() -> None:
    repo = JsonSpotRepository()
    spots = repo.all_spots()
    keys = [s.spot_key for s in spots]
    assert len(keys) == len(set(keys))  # no duplicates
    for spot in spots:
        assert spot.spot_key and spot.name
        assert -90.0 <= spot.lat <= 90.0
        assert -180.0 <= spot.lon <= 180.0


def test_bundled_dataset_has_geographic_spread() -> None:
    repo = JsonSpotRepository()
    countries = {s.country for s in repo.all_spots()}
    # The seed set spans multiple Atlantic surf countries so radius discovery
    # is demonstrable.
    assert {"Portugal", "Spain", "France", "Ireland"} <= countries


def test_repository_caches_parsed_spots() -> None:
    repo = JsonSpotRepository()
    first = repo.all_spots()
    second = repo.all_spots()
    assert [s.spot_key for s in first] == [s.spot_key for s in second]


# --------------------------------------------------------------------------- #
# JsonSpotRepository — radius discovery
# --------------------------------------------------------------------------- #
def test_spots_within_returns_expected_nearby_spots() -> None:
    repo = JsonSpotRepository()
    # Peniche, Portugal — Supertubos sits essentially on this point.
    peniche = GeoPoint(lat=39.3436, lon=-9.3577)
    nearby = repo.spots_within(peniche, radius_km=30.0)
    keys = {s.spot_key for s in nearby}
    assert "pt/peniche-supertubos" in keys
    # Mundaka (northern Spain) is far away and must not be included.
    assert "es/mundaka" not in keys


def test_spots_within_widening_radius_is_monotonic() -> None:
    repo = JsonSpotRepository()
    biarritz = GeoPoint(lat=43.4845, lon=-1.5586)
    near = {s.spot_key for s in repo.spots_within(biarritz, 20.0)}
    far = {s.spot_key for s in repo.spots_within(biarritz, 60.0)}
    assert near <= far
    # Hossegor/Seignosse are within ~25-30 km of Biarritz.
    assert "fr/hossegor-la-graviere" in far


# --------------------------------------------------------------------------- #
# JsonSpotRepository — explicit path + error handling
# --------------------------------------------------------------------------- #
def test_repository_reads_explicit_dataset_path(tmp_path: Path) -> None:
    data = [
        {"spot_key": "x/one", "name": "One", "lat": 0.0, "lon": 0.0},
        {"spot_key": "x/two", "name": "Two", "lat": 1.0, "lon": 1.0},
    ]
    path = tmp_path / "spots.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    repo = JsonSpotRepository(path)
    assert {s.spot_key for s in repo.all_spots()} == {"x/one", "x/two"}


def test_missing_dataset_path_raises(tmp_path: Path) -> None:
    repo = JsonSpotRepository(tmp_path / "does-not-exist.json")
    with pytest.raises(SpotDatasetError):
        repo.all_spots()


def test_malformed_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    repo = JsonSpotRepository(path)
    with pytest.raises(SpotDatasetError):
        repo.all_spots()


def test_non_array_dataset_raises(tmp_path: Path) -> None:
    path = tmp_path / "obj.json"
    path.write_text(json.dumps({"spot_key": "x"}), encoding="utf-8")
    repo = JsonSpotRepository(path)
    with pytest.raises(SpotDatasetError):
        repo.all_spots()


def test_duplicate_spot_key_raises(tmp_path: Path) -> None:
    data = [
        {"spot_key": "dup", "name": "A", "lat": 0.0, "lon": 0.0},
        {"spot_key": "dup", "name": "B", "lat": 1.0, "lon": 1.0},
    ]
    path = tmp_path / "dup.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    repo = JsonSpotRepository(path)
    with pytest.raises(SpotDatasetError):
        repo.all_spots()


# --------------------------------------------------------------------------- #
# SpotDiscoveryService
# --------------------------------------------------------------------------- #
class _FakeSpotRepository:
    """In-memory ``SpotRepository`` for service tests."""

    def __init__(self, spots: list[SurfSpot]) -> None:
        self._spots = spots

    def all_spots(self) -> list[SurfSpot]:
        return list(self._spots)

    def spots_within(self, center: GeoPoint, radius_km: float) -> list[SurfSpot]:
        from brizocast.core.domain.geo import spots_within as _within

        return _within(center, radius_km, self._spots)


def test_discovery_returns_nearby_spots() -> None:
    spots = [
        SurfSpot(spot_key="near", name="Near", lat=39.35, lon=-9.36),
        SurfSpot(spot_key="far", name="Far", lat=43.41, lon=-2.70),
    ]
    service = SpotDiscoveryService(_FakeSpotRepository(spots))
    result = service.discover(GeoPoint(lat=39.3436, lon=-9.3577), 30.0, subscription_id=7)
    assert isinstance(result, SpotDiscoveryResult)
    assert result.has_nearby_spots
    assert not result.is_empty
    assert {s.spot_key for s in result.spots} == {"near"}
    assert result.subscription_id == 7


def test_discovery_empty_triggers_skip_signal() -> None:
    spots = [SurfSpot(spot_key="far", name="Far", lat=43.41, lon=-2.70)]
    service = SpotDiscoveryService(_FakeSpotRepository(spots))
    result = service.discover(GeoPoint(lat=-33.0, lon=151.0), 25.0, subscription_id=1)
    assert result.is_empty
    assert not result.has_nearby_spots
    assert result.spots == ()


def test_discovery_against_real_repository() -> None:
    service = SpotDiscoveryService(JsonSpotRepository())
    result = service.discover(GeoPoint(lat=43.4845, lon=-1.5586), 40.0)
    assert result.has_nearby_spots
    assert "fr/biarritz-grande-plage" in {s.spot_key for s in result.spots}
