"""Service health tracker with write-through to shared DB.

Each time a real external call completes (Open-Meteo fetch, Gemini preset
generation, Surfline ingestion, Stormglass), the tracker records the outcome
and immediately persists the updated entry to the shared config_overrides table.
The admin panel reads that table on page load — no periodic job needed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

__all__ = ["HealthTracker", "ServiceStatus", "tracker"]

# Async callback type: receives (service_key, status_dict) and persists it.
PersistCallback = Callable[[str, dict[str, object]], Awaitable[None]]


@dataclass
class ServiceStatus:
    ok: bool = False
    message: str = "never called"
    last_call: datetime | None = None
    response_ms: float | None = None


class HealthTracker:
    """Tracks last-call status for external services with write-through to DB."""

    def __init__(self) -> None:
        self._services: dict[str, ServiceStatus] = {}
        self._persist: PersistCallback | None = None

    def set_persist_callback(self, callback: PersistCallback) -> None:
        """Wire in the async DB-write function (called once at app startup)."""
        self._persist = callback

    def record_success(
        self,
        service: str,
        *,
        message: str = "OK",
        response_ms: float | None = None,
    ) -> None:
        self._services[service] = ServiceStatus(
            ok=True,
            message=message,
            last_call=datetime.now(UTC),
            response_ms=response_ms,
        )
        self._flush(service)

    def record_failure(
        self,
        service: str,
        *,
        message: str = "failed",
        response_ms: float | None = None,
    ) -> None:
        self._services[service] = ServiceStatus(
            ok=False,
            message=message,
            last_call=datetime.now(UTC),
            response_ms=response_ms,
        )
        self._flush(service)
        self._flush(service)

    def get(self, service: str) -> ServiceStatus:
        return self._services.get(service, ServiceStatus())

    def _flush(self, service: str) -> None:
        """Fire-and-forget async write to DB (won't block the caller)."""
        if self._persist is None:
            return
        s = self._services[service]
        payload: dict[str, object] = {
            "ok": s.ok,
            "message": s.message,
            "last_call": s.last_call.isoformat() if s.last_call else None,
            "response_ms": s.response_ms,
        }
        # Schedule the coroutine on the running loop without awaiting it.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._persist(service, payload))
        except RuntimeError:
            pass  # No running loop (e.g. in tests) — skip persistence.


# Singleton used by the bot process.
tracker: Final[HealthTracker] = HealthTracker()
