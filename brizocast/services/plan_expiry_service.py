"""Periodic plan-expiry check: flip lapsed Paid plans to expired (Req 20.7).

``PlanExpiryService`` is the small, isolated job that enforces the lifecycle
rule of Requirement 20.7: *when a Paid plan's expiry time passes, its
``Plan_Status`` becomes expired*. It is intentionally separate from
:class:`~brizocast.services.entitlement_service.EntitlementService` (which only
*reads* a plan to gate subscription creation): this service is the single place
that *writes* the expiry transition, and it is shaped to be run periodically by
the scheduler.

What it does
------------
:meth:`PlanExpiryService.run` opens one unit of work, asks the plan repository
for the Paid plans that are still ``active`` but whose ``expiry_at`` is strictly
earlier than the injected ``now`` (see
:meth:`~brizocast.repositories.plan_repo.SqlAlchemyPlanRepository.list_paid_active_expired`),
sets each of their statuses to :attr:`~brizocast.models.plan.PlanStatus.EXPIRED`,
and persists the change. It returns the number of plans transitioned so callers
(and tests) can observe the work done. Because the repository query already
excludes Free plans (no expiry), future-dated plans, and already
expired/canceled plans, the check is **idempotent**: running it again with the
same clock transitions nothing.

Injection / scheduling
-----------------------
Following the service-layer unit-of-work convention (see
:class:`~brizocast.services.user_service.UserService`), the service is injected
with the application's ``async_sessionmaker`` and a monotonic ``now`` clock so
the "current time" is deterministic in tests. The composition root (task 11.1)
and the scheduler (task 8.3) run the check periodically; this service exposes
the work as an **injectable callable** — both :meth:`run` and ``__call__``
return the transition count — so the scheduler can register
``plan_expiry_service`` (or ``plan_expiry_service.run``) directly as a job
without this module knowing anything about APScheduler.

Requirements covered: 20.7 (supports Property 30).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.core.logging import BoundLogger, get_logger
from brizocast.database.session import session_scope
from brizocast.models.plan import PlanStatus
from brizocast.repositories.plan_repo import SqlAlchemyPlanRepository

__all__ = ["PlanExpiryService"]


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class PlanExpiryService:
    """Transition lapsed Paid plans to expired on a periodic check (Req 20.7)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime] = _utc_now,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            session_factory: The application's ``async_sessionmaker``. Each run
                opens one session via ``session_scope`` so the reads and the
                status updates share a single transaction.
            now: Clock returning the "current time" the expiry is measured
                against; injected for deterministic tests.
            logger: Optional bound logger; one is created when omitted.
        """
        self._session_factory = session_factory
        self._now = now
        self._log = logger or get_logger(__name__)

    async def run(self) -> int:
        """Expire every active Paid plan whose ``expiry_at`` has passed.

        Resolves the "current time" from the injected clock, finds the active
        Paid plans whose expiry is strictly earlier than that time, sets each to
        :attr:`PlanStatus.EXPIRED`, and persists the changes in one transaction
        (Req 20.7). Idempotent: a second run with the same clock finds nothing
        left to expire.

        Returns:
            The number of plans transitioned to expired.
        """
        now = self._now()
        async with session_scope(self._session_factory) as session:
            plans = SqlAlchemyPlanRepository(session)
            lapsed = await plans.list_paid_active_expired(now)
            for plan in lapsed:
                plan.status = PlanStatus.EXPIRED
                await plans.update(plan)
        if lapsed:
            self._log.info(
                "plan-expiry check expired %s paid plan(s) as of %s",
                len(lapsed),
                now.isoformat(),
            )
        return len(lapsed)

    async def __call__(self) -> int:
        """Run the expiry check; alias of :meth:`run` for use as a scheduled job.

        Lets the composition root register the service instance directly as an
        injectable callable (e.g. an APScheduler job) without exposing scheduler
        details here.
        """
        return await self.run()
