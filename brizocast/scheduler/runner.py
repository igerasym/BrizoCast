"""APScheduler runner: interval guard + completion-timestamp semantics (task 8.3).

:class:`SchedulerRunner` is the orchestration layer that wires the periodic
forecast-check job (task 8.1) and the three digest jobs (task 8.5) onto an
``AsyncIOScheduler`` and enforces the scheduling rules of Requirement 14
(supports Property 25). It owns **no** business rule: it decides *when* the jobs
run and, for the forecast-check job, *whether* a completion timestamp is
recorded — delegating the actual pass to :meth:`ForecastCheckJob.run_once`.

Triggers (Req 14.1)
-------------------
* The forecast-check job is registered on an :class:`IntervalTrigger` at
  ``Settings.SCHEDULER_INTERVAL_MINUTES`` minutes.
* The morning and evening digests are registered on a daily
  :class:`CronTrigger` parsed from ``MORNING_DIGEST_TIME`` / ``EVENING_DIGEST_TIME``
  (``"HH:MM"``).
* The weekly-best-day digest is registered on a weekly :class:`CronTrigger`
  parsed from ``WEEKLY_DIGEST`` (``"DAY HH:MM"``, e.g. ``"MON 07:00"``).

Interval guard (Req 14.7)
-------------------------
Two layers cooperate so a new forecast-check pass never starts before the
configured interval has elapsed since the previous run:

#. **Scheduler level.** The forecast-check job is added with
   ``max_instances=1`` and ``coalesce=True``. ``IntervalTrigger`` itself spaces
   fire times exactly one interval apart (the next fire time is the previous
   fire time plus the interval), ``max_instances=1`` prevents a second instance
   from starting while one is still running, and ``coalesce=True`` collapses any
   fire times missed while the process was busy/asleep into a single run rather
   than a catch-up burst.
#. **Explicit guard.** The wrapped callable additionally records the wall-clock
   time at which it last *started* a pass and, on entry, skips when
   ``now - last_started < interval``. This makes the guarantee independent of the
   trigger (so a manual/early invocation or a misconfigured trigger cannot start
   an early pass) and is the directly unit-testable expression of Req 14.7 /
   Property 25: *for any current time earlier than the previous run plus the
   configured interval, no new forecast-check job starts.*

Completion-timestamp semantics (Req 14.4, 14.5, 14.6)
-----------------------------------------------------
The runner records the run timestamp through the injected
:class:`SchedulerRunRecorder` **only after** ``run_once`` returns normally:

* **Success (Req 14.4).** When the pass completes, the recorder is called with a
  fresh completion timestamp.
* **Failure before completion (Req 14.5).** When ``run_once`` raises (e.g.
  loading the active subscriptions failed), the exception is logged and the
  recorder is **not** called, so the previous last-run timestamp is left
  unchanged.
* **Timestamp-write failure (Req 14.6).** The ``record_success_async`` call is
  wrapped in ``try``/``except``: if recording the timestamp itself fails while
  the job
  otherwise succeeded, the failure is logged and swallowed and the job is still
  treated as completed.

Scope boundary
--------------
This module does not construct the jobs, services, or the shared scheduler-run
state — the composition root (task 11.1) builds those and injects them, then
calls :meth:`register_jobs` (or :meth:`start`, which registers on first use).
The scheduler instance itself is injectable so tests can drive registration with
a fake and exercise the wrapped forecast-check callable directly without real
scheduling.

Requirements covered: 14.1, 14.4, 14.5, 14.6, 14.7 (supports Property 25).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from brizocast.config.settings import Settings
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.scheduler.digest_jobs import DigestJobRunner
from brizocast.scheduler.forecast_check_job import ForecastCheckJob
from brizocast.services.scheduler_state import SchedulerRunRecorder

__all__ = [
    "FORECAST_CHECK_JOB_ID",
    "EVENING_DIGEST_JOB_ID",
    "MORNING_DIGEST_JOB_ID",
    "WEEKLY_DIGEST_JOB_ID",
    "SchedulerLike",
    "SchedulerRunner",
]

# Stable APScheduler job ids. Used with ``replace_existing=True`` so re-running
# ``register_jobs`` (e.g. after a restart) overwrites rather than duplicates.
FORECAST_CHECK_JOB_ID = "forecast-check"
MORNING_DIGEST_JOB_ID = "morning-digest"
EVENING_DIGEST_JOB_ID = "evening-digest"
WEEKLY_DIGEST_JOB_ID = "weekly-digest"

# APScheduler ``CronTrigger`` day-of-week tokens (also accepts 0-6, mon-first).
_WEEKDAY_TOKENS = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


@runtime_checkable
class SchedulerLike(Protocol):
    """The minimal slice of ``AsyncIOScheduler`` the runner depends on.

    Declared as a Protocol so the runner is decoupled from APScheduler's
    concrete types: production injects (or defaults to) an
    :class:`AsyncIOScheduler`, while tests inject a trivial fake that records the
    jobs registered on it.
    """

    def add_job(
        self,
        func: Callable[..., object],
        trigger: object = ...,
        *,
        id: str | None = ...,
        max_instances: int = ...,
        coalesce: bool = ...,
        replace_existing: bool = ...,
    ) -> object:
        """Register ``func`` to run on ``trigger``."""
        ...

    def start(self, paused: bool = ...) -> None:
        """Start processing scheduled jobs."""
        ...

    def shutdown(self, wait: bool = ...) -> None:
        """Stop the scheduler, optionally waiting for running jobs."""
        ...


@dataclass(frozen=True, slots=True)
class _TimeOfDay:
    """A parsed ``HH:MM`` clock time used to build a daily cron trigger."""

    hour: int
    minute: int


@dataclass(frozen=True, slots=True)
class _WeeklyTime:
    """A parsed ``DAY HH:MM`` weekly time used to build a weekly cron trigger."""

    day_of_week: str
    hour: int
    minute: int


def _parse_hour_minute(value: str) -> _TimeOfDay:
    """Parse a ``"HH:MM"`` string into a validated :class:`_TimeOfDay`.

    Args:
        value: The clock time, e.g. ``"07:00"``.

    Raises:
        ValueError: If the string is not ``HH:MM`` or the hour/minute is out of
            range. The runner surfaces this at startup rather than registering a
            silently-wrong trigger.
    """
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected a 'HH:MM' time, got {value!r}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"non-numeric time component in {value!r}") from exc
    if not 0 <= hour <= 23:
        raise ValueError(f"hour out of range in {value!r}")
    if not 0 <= minute <= 59:
        raise ValueError(f"minute out of range in {value!r}")
    return _TimeOfDay(hour=hour, minute=minute)


def _parse_weekly(value: str) -> _WeeklyTime:
    """Parse a ``"DAY HH:MM"`` string into a validated :class:`_WeeklyTime`.

    The day token is matched case-insensitively against the three-letter weekday
    abbreviations APScheduler's :class:`CronTrigger` understands (``mon``..``sun``).

    Args:
        value: The weekly time, e.g. ``"MON 07:00"``.

    Raises:
        ValueError: If the string is not ``DAY HH:MM`` or the day is not a known
            weekday token.
    """
    tokens = value.strip().split()
    if len(tokens) != 2:
        raise ValueError(f"expected a 'DAY HH:MM' weekly time, got {value!r}")
    day = tokens[0].lower()
    if day not in _WEEKDAY_TOKENS:
        raise ValueError(
            f"unknown weekday {tokens[0]!r} in {value!r}; "
            f"expected one of {sorted(_WEEKDAY_TOKENS)}"
        )
    clock = _parse_hour_minute(tokens[1])
    return _WeeklyTime(day_of_week=day, hour=clock.hour, minute=clock.minute)


class SchedulerRunner:
    """Wires the forecast-check and digest jobs onto an ``AsyncIOScheduler``.

    Enforces the forecast-check interval guard (Req 14.7) and the completion
    timestamp semantics (Req 14.4-14.6). Build it at the composition root with
    the jobs, the shared scheduler-run recorder, and the settings, then call
    :meth:`start` (which registers the jobs on first use) — or :meth:`register_jobs`
    explicitly when you want to register without starting.
    """

    def __init__(
        self,
        forecast_job: ForecastCheckJob,
        digest_runner: DigestJobRunner,
        run_recorder: SchedulerRunRecorder,
        settings: Settings,
        *,
        scheduler: SchedulerLike | None = None,
        now: Callable[[], datetime] = _utc_now,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the runner.

        Args:
            forecast_job: The periodic forecast-check job; its ``run_once`` is
                wrapped by the interval guard and timestamp semantics.
            digest_runner: Provides the morning/evening/weekly digest callables.
            run_recorder: Shared write side of the scheduler-run state; receives
                the completion timestamp on a successful pass (Req 14.4).
            settings: Validated configuration supplying the interval and the
                three digest trigger times.
            scheduler: APScheduler-like instance to register jobs on; a new
                :class:`AsyncIOScheduler` is created when omitted. Injected for
                testing.
            now: Clock returning the current time; injected for deterministic
                tests of the interval guard and timestamp semantics.
            logger: Optional bound logger; one is created when omitted.
        """
        self._forecast_job = forecast_job
        self._digest_runner = digest_runner
        self._recorder = run_recorder
        self._settings = settings
        self._scheduler: SchedulerLike = scheduler or AsyncIOScheduler()
        self._now = now
        self._log = logger or get_logger(__name__)

        self._interval = timedelta(minutes=settings.SCHEDULER_INTERVAL_MINUTES)
        # Wall-clock time the most recent forecast-check pass *started*; the
        # explicit interval guard's reference point (Req 14.7). ``None`` until
        # the first pass starts.
        self._last_started: datetime | None = None
        self._jobs_registered = False

    # -- lifecycle ------------------------------------------------------- #
    def register_jobs(self) -> None:
        """Register the forecast-check and three digest jobs on the scheduler.

        Idempotent: every job is added with a stable id and
        ``replace_existing=True`` so a second call (e.g. after a restart)
        overwrites rather than duplicates. Trigger times are parsed here so a
        malformed digest time fails loudly at registration rather than firing a
        wrong trigger later.
        """
        interval_minutes = self._settings.SCHEDULER_INTERVAL_MINUTES
        self._scheduler.add_job(
            self._run_forecast_check,
            IntervalTrigger(minutes=interval_minutes),
            id=FORECAST_CHECK_JOB_ID,
            # Req 14.7 — never overlap; collapse missed fire times into one run.
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

        morning = _parse_hour_minute(self._settings.MORNING_DIGEST_TIME)
        self._scheduler.add_job(
            self._digest_runner.run_morning_digest,
            CronTrigger(hour=morning.hour, minute=morning.minute),
            id=MORNING_DIGEST_JOB_ID,
            replace_existing=True,
        )

        evening = _parse_hour_minute(self._settings.EVENING_DIGEST_TIME)
        self._scheduler.add_job(
            self._digest_runner.run_evening_digest,
            CronTrigger(hour=evening.hour, minute=evening.minute),
            id=EVENING_DIGEST_JOB_ID,
            replace_existing=True,
        )

        weekly = _parse_weekly(self._settings.WEEKLY_DIGEST)
        self._scheduler.add_job(
            self._digest_runner.run_weekly_digest,
            CronTrigger(
                day_of_week=weekly.day_of_week,
                hour=weekly.hour,
                minute=weekly.minute,
            ),
            id=WEEKLY_DIGEST_JOB_ID,
            replace_existing=True,
        )

        self._jobs_registered = True
        self._log.info(
            "scheduler jobs registered: forecast-check every %d min; "
            "morning %s, evening %s, weekly %s",
            interval_minutes,
            self._settings.MORNING_DIGEST_TIME,
            self._settings.EVENING_DIGEST_TIME,
            self._settings.WEEKLY_DIGEST,
        )

    def start(self) -> None:
        """Register the jobs (if needed) and start the scheduler."""
        if not self._jobs_registered:
            self.register_jobs()
        self._scheduler.start()
        self._log.info("scheduler started")

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop the scheduler, optionally waiting for a running job to finish."""
        self._scheduler.shutdown(wait=wait)
        self._log.info("scheduler stopped")

    # -- the wrapped forecast-check pass --------------------------------- #
    async def _run_forecast_check(self) -> None:
        """Run one guarded forecast-check pass with completion-timestamp semantics.

        Applies the explicit interval guard (Req 14.7) before delegating to
        :meth:`ForecastCheckJob.run_once`, then records the completion timestamp
        only on success (Req 14.4), leaves it unchanged on a pre-completion
        failure (Req 14.5), and treats the job as completed while logging a
        timestamp-write failure (Req 14.6).
        """
        now = self._now()

        # Req 14.7 — do not start a new pass before the interval has elapsed.
        if self._last_started is not None:
            elapsed = now - self._last_started
            if elapsed < self._interval:
                self._log.info(
                    "interval not elapsed since last run (%s < %s); skipping pass",
                    elapsed,
                    self._interval,
                )
                return

        # Mark the start *before* running so an overlapping/early re-entry is
        # guarded even while this pass is in flight.
        self._last_started = now

        try:
            await self._forecast_job.run_once()
        except Exception:  # noqa: BLE001 - a failed pass must not crash the loop.
            # Req 14.5 — started but did not complete: leave the timestamp as-is.
            self._log.exception(
                "forecast-check job failed before completion; "
                "last-run timestamp left unchanged"
            )
            return

        # Req 14.4 — completed successfully: record the completion timestamp.
        completion = self._now()
        try:
            await self._recorder.record_success_async(completion)
        except Exception:  # noqa: BLE001 - Req 14.6.
            # The job itself succeeded; only the timestamp write failed. Treat
            # the job as completed and log the write failure.
            self._log.exception(
                "forecast-check job completed but recording the run "
                "timestamp failed; treating the job as completed"
            )
