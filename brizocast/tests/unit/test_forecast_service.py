"""Unit tests for :class:`ForecastService` cache logic (task 3.8, Req 7).

These tests exercise the service against an in-memory fake cache repository and
a call-counting fake forecast provider, verifying that:

* a fresh (non-expired) cached entry is served without calling the provider
  (Req 7.2);
* a cache miss calls the provider exactly once and stores the result with
  ``expires_at = fetched_at + TTL`` (Req 7.3, 7.4);
* an expired entry triggers a single refetch (Req 7.4);
* multiple subscriptions referencing the same spot share one cache entry, so the
  provider is called at most once per TTL window (Req 7.1, 7.5);
* a :class:`ProviderRequestError` propagates to the caller (Req 6.5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brizocast.core.domain.forecast import Forecast, ForecastStep, ForecastWindow
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.errors import ProviderRequestError
from brizocast.models.forecast_cache import ForecastCache
from brizocast.services.forecast_service import ForecastService

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #
def _make_forecast(spot_key: str, *, wave: float = 1.5) -> Forecast:
    """Build a minimal one-step forecast for ``spot_key``."""
    return Forecast(
        spot_id=spot_key,
        steps=[
            ForecastStep(
                timestamp=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
                wave_height_m=wave,
                swell_period_s=11.0,
                swell_direction_deg=300.0,
                wind_speed_kmh=8.0,
                wind_direction_deg=90.0,
            )
        ],
    )


def _window() -> ForecastWindow:
    return ForecastWindow(
        start=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
        end=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
    )


def _spot(spot_key: str = "pt/peniche-supertubos") -> SurfSpot:
    return SurfSpot(spot_key=spot_key, name="Supertubos", lat=39.3436, lon=-9.3577)


class _FakeForecastProvider:
    """Call-counting fake provider returning a per-spot forecast."""

    key = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def get_forecast(
        self, lat: float, lon: float, window: ForecastWindow
    ) -> Forecast:
        self.calls += 1
        # Encode the call count in the wave height so refetches are detectable.
        return _make_forecast(spot_key=f"{lat:.4f},{lon:.4f}", wave=float(self.calls))


class _FailingForecastProvider:
    """Fake provider that always fails with a ``ProviderRequestError``."""

    key = "boom"

    def __init__(self) -> None:
        self.calls = 0

    async def get_forecast(
        self, lat: float, lon: float, window: ForecastWindow
    ) -> Forecast:
        self.calls += 1
        raise ProviderRequestError("network down", provider=self.key)


class _FakeCacheRepository:
    """In-memory ``ForecastCacheRepository`` keyed by ``spot_key``."""

    def __init__(self) -> None:
        self._entries: dict[str, ForecastCache] = {}
        self.put_calls = 0

    async def get(self, spot_key: str) -> ForecastCache | None:
        return self._entries.get(spot_key)

    async def put(
        self,
        spot_key: str,
        forecast: Forecast,
        fetched_at: datetime,
        expires_at: datetime,
    ) -> None:
        self.put_calls += 1
        self._entries[spot_key] = ForecastCache(
            spot_key=spot_key,
            forecast_payload=forecast.model_dump(mode="json"),
            fetched_at=fetched_at,
            expires_at=expires_at,
        )

    async def delete_expired(self, now: datetime) -> int:
        expired = [k for k, v in self._entries.items() if v.expires_at <= now]
        for key in expired:
            del self._entries[key]
        return len(expired)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_cache_miss_calls_provider_once_and_stores() -> None:
    provider = _FakeForecastProvider()
    cache = _FakeCacheRepository()
    base = datetime(2025, 6, 1, 5, 0, tzinfo=UTC)
    ttl = timedelta(minutes=180)
    service = ForecastService(provider, cache, ttl, now=lambda: base)

    spot = _spot()
    forecast = await service.get_forecast(spot, _window())

    assert provider.calls == 1
    assert cache.put_calls == 1
    stored = await cache.get(spot.spot_key)
    assert stored is not None
    assert stored.fetched_at == base
    assert stored.expires_at == base + ttl
    assert Forecast.model_validate(stored.forecast_payload) == forecast


async def test_fresh_cache_hit_does_not_call_provider() -> None:
    provider = _FakeForecastProvider()
    cache = _FakeCacheRepository()
    base = datetime(2025, 6, 1, 5, 0, tzinfo=UTC)
    ttl = timedelta(minutes=180)
    clock = {"now": base}
    service = ForecastService(provider, cache, ttl, now=lambda: clock["now"])

    spot = _spot()
    first = await service.get_forecast(spot, _window())
    assert provider.calls == 1

    # Advance time but stay within the TTL window.
    clock["now"] = base + timedelta(minutes=179)
    second = await service.get_forecast(spot, _window())

    assert provider.calls == 1  # provider not called again (Req 7.2)
    assert cache.put_calls == 1
    assert second == first


async def test_expired_entry_triggers_single_refetch() -> None:
    provider = _FakeForecastProvider()
    cache = _FakeCacheRepository()
    base = datetime(2025, 6, 1, 5, 0, tzinfo=UTC)
    ttl = timedelta(minutes=180)
    clock = {"now": base}
    service = ForecastService(provider, cache, ttl, now=lambda: clock["now"])

    spot = _spot()
    await service.get_forecast(spot, _window())
    assert provider.calls == 1

    # Move to exactly the expiry instant: expires_at is treated as expired.
    clock["now"] = base + ttl
    await service.get_forecast(spot, _window())

    assert provider.calls == 2  # refetched once (Req 7.4)
    assert cache.put_calls == 2
    stored = await cache.get(spot.spot_key)
    assert stored is not None
    assert stored.fetched_at == base + ttl
    assert stored.expires_at == base + ttl + ttl


async def test_multiple_subscriptions_share_one_provider_call_per_ttl() -> None:
    provider = _FakeForecastProvider()
    cache = _FakeCacheRepository()
    base = datetime(2025, 6, 1, 5, 0, tzinfo=UTC)
    ttl = timedelta(minutes=180)
    clock = {"now": base}
    service = ForecastService(provider, cache, ttl, now=lambda: clock["now"])

    spot = _spot()

    # Three different subscriptions reference the same spot within one TTL.
    results = []
    for offset in (0, 30, 90):
        clock["now"] = base + timedelta(minutes=offset)
        results.append(await service.get_forecast(spot, _window()))

    assert provider.calls == 1  # at most once per TTL window (Req 7.5)
    assert all(r == results[0] for r in results)


async def test_distinct_spots_each_get_their_own_entry() -> None:
    provider = _FakeForecastProvider()
    cache = _FakeCacheRepository()
    base = datetime(2025, 6, 1, 5, 0, tzinfo=UTC)
    service = ForecastService(
        provider, cache, timedelta(minutes=180), now=lambda: base
    )

    await service.get_forecast(_spot("a/one"), _window())
    await service.get_forecast(_spot("b/two"), _window())

    assert provider.calls == 2
    assert await cache.get("a/one") is not None
    assert await cache.get("b/two") is not None


async def test_provider_error_propagates_and_nothing_cached() -> None:
    provider = _FailingForecastProvider()
    cache = _FakeCacheRepository()
    base = datetime(2025, 6, 1, 5, 0, tzinfo=UTC)
    service = ForecastService(
        provider, cache, timedelta(minutes=180), now=lambda: base
    )

    spot = _spot()
    with pytest.raises(ProviderRequestError):
        await service.get_forecast(spot, _window())

    assert provider.calls == 1
    assert cache.put_calls == 0
    assert await cache.get(spot.spot_key) is None
