"""Forecast retrieval service wrapping a provider with a per-spot TTL cache.

``ForecastService`` is the application-layer use case that supplies a spot's
:class:`~brizocast.core.domain.forecast.Forecast` while shielding the configured
``ForecastProvider`` behind the shared per-spot ``Forecast_Cache`` (Req 7):

* When a non-expired cached entry exists for the spot, the cached forecast is
  returned **without** calling the provider (Req 7.2).
* Otherwise the provider is called exactly once, the result is stored with
  ``expires_at = fetched_at + TTL``, and the fresh forecast is returned
  (Req 7.3, 7.4).
* Because entries are keyed by ``spot_key``, every subscription that references
  the same spot shares one cached forecast, so the provider is hit at most once
  per spot per TTL window (Req 7.1, 7.5).

The cache is keyed solely by ``spot_key``; the coordinates of the spot are used
only when a provider call is actually required. The provider, cache repository,
TTL, and a ``now`` clock are all injected, keeping the service deterministic and
unit-testable with fakes.

Provider-failure policy
-----------------------
A :class:`~brizocast.core.errors.ProviderRequestError` raised by the provider is
**allowed to propagate**. The scheduler's forecast-check job (task 8.1) catches
it, logs the failure with provider context, and skips the affected spot for that
run (Req 6.5), so this service does not swallow the error or cache a partial
result.

Requirements covered: 7.1, 7.2, 7.3, 7.4, 7.5.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from brizocast.core.domain.forecast import Forecast, ForecastWindow
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.core.ports.forecast_provider import ForecastProvider
from brizocast.core.ports.repositories import ForecastCacheRepository

__all__ = ["ForecastService"]


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class ForecastService:
    """Supplies forecasts via a shared per-spot TTL cache (Req 7)."""

    def __init__(
        self,
        provider: ForecastProvider,
        cache_repository: ForecastCacheRepository,
        ttl: timedelta,
        *,
        now: Callable[[], datetime] = _utc_now,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            provider: The forecast provider port called on a cache miss.
            cache_repository: The per-spot forecast cache repository port.
            ttl: Cache time-to-live, typically
                ``timedelta(minutes=Settings.FORECAST_CACHE_TTL_MINUTES)``.
            now: Clock returning the current time; injected for testability.
            logger: Optional bound logger; one is created when omitted.
        """
        self._provider = provider
        self._cache = cache_repository
        self._ttl = ttl
        self._now = now
        self._log = logger or get_logger(__name__)

    def set_provider(self, provider: ForecastProvider) -> None:
        """Swap the active forecast provider (Req 7.3).

        Used by the scheduler's forecast-check job to apply a live provider
        switch resolved via
        :class:`~brizocast.services.provider_selector.ProviderSelector` at the
        start of a run, so a change made in the admin panel takes effect on the
        next tick without restarting the bot. Cached forecasts already stored
        remain valid until their TTL elapses; the new provider is used on the
        next cache miss.
        """
        self._provider = provider

    async def get_forecast(self, spot: SurfSpot, window: ForecastWindow) -> Forecast:
        """Return the forecast for ``spot`` over ``window`` (Req 7.2, 7.3).

        Returns a non-expired cached forecast without calling the provider; on a
        miss or an expired entry, calls the provider once, stores the result
        with ``expires_at = fetched_at + TTL``, and returns it. A
        :class:`~brizocast.core.errors.ProviderRequestError` from the provider
        propagates to the caller (Req 6.5).
        """
        now = self._now()
        log = self._log.bind(spot_key=spot.spot_key, provider=self._provider.key)

        cached = await self._cache.get(spot.spot_key)
        if cached is not None and now < cached.expires_at:
            # Fresh entry: serve from cache, shared across subscriptions
            # (Req 7.2, 7.5).
            log.debug("forecast cache hit (expires_at=%s)", cached.expires_at)
            return Forecast.model_validate(cached.forecast_payload)

        # Miss or expired entry: fetch once and store (Req 7.3, 7.4).
        if cached is None:
            log.debug("forecast cache miss; fetching from provider")
        else:
            log.debug("forecast cache expired; refetching from provider")

        import time as _time

        from brizocast.services.health_tracker import tracker

        _t0 = _time.monotonic()
        try:
            forecast = await self._provider.get_forecast(spot.lat, spot.lon, window)
        except Exception:
            _elapsed_ms = (_time.monotonic() - _t0) * 1000.0
            tracker.record_failure(
                f"forecast:{self._provider.key}",
                message=f"failed for {spot.spot_key}",
                response_ms=_elapsed_ms,
            )
            raise
        _elapsed_ms = (_time.monotonic() - _t0) * 1000.0
        tracker.record_success(
            f"forecast:{self._provider.key}",
            message=f"{len(forecast.steps)} steps for {spot.spot_key}",
            response_ms=_elapsed_ms,
        )
        expires_at = now + self._ttl
        await self._cache.put(
            spot.spot_key,
            forecast,
            fetched_at=now,
            expires_at=expires_at,
        )
        log.info(
            "forecast fetched: %d step(s) in %.0f ms (cached until %s)",
            len(forecast.steps),
            _elapsed_ms,
            expires_at.isoformat(timespec="minutes"),
        )
        return forecast
