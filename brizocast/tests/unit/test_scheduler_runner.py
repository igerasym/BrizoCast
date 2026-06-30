"""Unit tests for :class:`SchedulerRunner` (task 8.3, supports Property 25).

Exercise the runner's two responsibilities against in-memory fakes and a
controllable clock, driving the wrapped forecast-check callable directly rather
than through real scheduling:

* completion-timestamp semantics — a successful pass records the completion
  timestamp (Req 14.4); a pass that raises before completing leaves it unchanged
  (Req 14.5); a timestamp-write failure is swallowed and the job still counts as
  completed (Req 14.6);
* the interval guard — a second pass started before the configured interval has
  elapsed is skipped (Req 14.7);

plus job registration: the forecast-check interval job and the three digest cron
jobs are registered with their expected ids and overlap-prevention options
(Req 14.1), and a malformed digest trigger time fails loudly at registration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from brizocast.config.settings import Settings
from brizocast.scheduler.forecast_check_job import ForecastCheckResult
from brizocast.scheduler.runner import (
    EVENING_DIGEST_JOB_ID,
    FORECAST_CHECK_JOB_ID,
    MORNING_DIGEST_JOB_ID,
    WEEKLY_DIGEST_JOB_ID,
    SchedulerRunner,
)
from brizocast.services.scheduler_state import InMemorySchedulerState

pytestmark = pytest.mark.unit

_START = datetime(2025, 6, 1, 5, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Clock:
    """A mutable clock returning the time it is set to; advanceable in tests."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, value: datetime) -> None:
        self._now = value


class _FakeForecastJob:
    """Forecast-check job whose ``run_once`` succeeds or raises on demand."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls = 0

    async def run_once(self) -> ForecastCheckResult:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return ForecastCheckResult(subscriptions_total=0)


class _FakeRecorder:
    """Scheduler-run recorder that captures writes; can be made to raise."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.recorded: list[datetime] = []

    async def record_success_async(self, when: datetime) -> None:
        if self._error is not None:
            raise self._error
        self.recorded.append(when)


@dataclass
class _AddedJob:
    """One captured ``add_job`` registration."""

    func: Callable[..., object]
    trigger: object
    id: str | None
    kwargs: dict[str, object]


@dataclass
class _FakeScheduler:
    """Records ``add_job`` registrations; ``start``/``shutdown`` are no-ops."""

    jobs: list[_AddedJob] = field(default_factory=list)
    started: bool = False
    shutdown_called: bool = False

    def add_job(
        self,
        func: Callable[..., object],
        trigger: object = None,
        *,
        id: str | None = None,
        max_instances: int = 1,
        coalesce: bool = False,
        replace_existing: bool = False,
    ) -> object:
        self.jobs.append(
            _AddedJob(
                func=func,
                trigger=trigger,
                id=id,
                kwargs={
                    "max_instances": max_instances,
                    "coalesce": coalesce,
                    "replace_existing": replace_existing,
                },
            )
        )
        return object()

    def start(self, paused: bool = False) -> None:
        self.started = True

    def shutdown(self, wait: bool = True) -> None:
        self.shutdown_called = True

    def by_id(self, job_id: str) -> _AddedJob:
        return next(job for job in self.jobs if job.id == job_id)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "SCHEDULER_INTERVAL_MINUTES": 60,
        "MORNING_DIGEST_TIME": "07:00",
        "EVENING_DIGEST_TIME": "18:00",
        "WEEKLY_DIGEST": "MON 07:00",
    }
    base.update(overrides)
    return Settings(**base)


def _runner(
    *,
    forecast_job: _FakeForecastJob,
    recorder: _FakeRecorder,
    clock: _Clock,
    scheduler: _FakeScheduler | None = None,
    settings: Settings | None = None,
) -> SchedulerRunner:
    return SchedulerRunner(
        forecast_job,  # type: ignore[arg-type]
        _digest_runner_stub(),
        recorder,
        settings or _settings(),
        scheduler=scheduler or _FakeScheduler(),
        now=clock,
    )


def _digest_runner_stub() -> Any:
    """A stand-in digest runner exposing the three callables the runner wires."""

    class _Stub:
        async def run_morning_digest(self) -> list[Any]:
            return []

        async def run_evening_digest(self) -> list[Any]:
            return []

        async def run_weekly_digest(self) -> list[Any]:
            return []

    return _Stub()


# --------------------------------------------------------------------------- #
# Completion-timestamp semantics (Req 14.4, 14.5, 14.6)
# --------------------------------------------------------------------------- #
async def test_successful_pass_records_completion_timestamp() -> None:
    clock = _Clock(_START)
    forecast_job = _FakeForecastJob()
    recorder = _FakeRecorder()
    runner = _runner(forecast_job=forecast_job, recorder=recorder, clock=clock)

    await runner._run_forecast_check()

    assert forecast_job.calls == 1
    assert recorder.recorded == [_START]  # Req 14.4


async def test_failing_pass_leaves_timestamp_unchanged() -> None:
    clock = _Clock(_START)
    forecast_job = _FakeForecastJob(error=RuntimeError("load failed"))
    recorder = _FakeRecorder()
    runner = _runner(forecast_job=forecast_job, recorder=recorder, clock=clock)

    await runner._run_forecast_check()

    # Started but did not complete -> recorder never called (Req 14.5).
    assert forecast_job.calls == 1
    assert recorder.recorded == []


async def test_timestamp_write_failure_is_swallowed_job_still_completes() -> None:
    clock = _Clock(_START)
    forecast_job = _FakeForecastJob()
    recorder = _FakeRecorder(error=RuntimeError("disk full"))
    runner = _runner(forecast_job=forecast_job, recorder=recorder, clock=clock)

    # Req 14.6 — the timestamp write fails but does not propagate.
    await runner._run_forecast_check()

    assert forecast_job.calls == 1  # the job ran and is treated as completed
    assert recorder.recorded == []


async def test_completion_timestamp_uses_post_run_clock() -> None:
    # The recorded timestamp is the *completion* time, not the start time.
    times = iter([_START, _START + timedelta(minutes=3)])

    def clock() -> datetime:
        return next(times)

    forecast_job = _FakeForecastJob()
    recorder = _FakeRecorder()
    runner = SchedulerRunner(
        forecast_job,  # type: ignore[arg-type]
        _digest_runner_stub(),
        recorder,
        _settings(),
        scheduler=_FakeScheduler(),
        now=clock,
    )

    await runner._run_forecast_check()

    assert recorder.recorded == [_START + timedelta(minutes=3)]


# --------------------------------------------------------------------------- #
# Interval guard (Req 14.7)
# --------------------------------------------------------------------------- #
async def test_interval_guard_skips_early_second_start() -> None:
    clock = _Clock(_START)
    forecast_job = _FakeForecastJob()
    recorder = _FakeRecorder()
    runner = _runner(forecast_job=forecast_job, recorder=recorder, clock=clock)

    await runner._run_forecast_check()  # first pass runs
    clock.advance(timedelta(minutes=30))  # < 60-minute interval
    await runner._run_forecast_check()  # should be skipped (Req 14.7)

    assert forecast_job.calls == 1
    assert recorder.recorded == [_START]


async def test_second_pass_runs_once_interval_elapsed() -> None:
    clock = _Clock(_START)
    forecast_job = _FakeForecastJob()
    recorder = _FakeRecorder()
    runner = _runner(forecast_job=forecast_job, recorder=recorder, clock=clock)

    await runner._run_forecast_check()
    clock.advance(timedelta(minutes=60))  # exactly the interval has elapsed
    await runner._run_forecast_check()

    assert forecast_job.calls == 2
    assert recorder.recorded == [_START, _START + timedelta(minutes=60)]


async def test_failed_pass_still_arms_interval_guard() -> None:
    # A pass that starts but fails marks the start time, so an immediate retry
    # within the interval is still suppressed (Req 14.5 + 14.7).
    clock = _Clock(_START)
    forecast_job = _FakeForecastJob(error=RuntimeError("boom"))
    recorder = _FakeRecorder()
    runner = _runner(forecast_job=forecast_job, recorder=recorder, clock=clock)

    await runner._run_forecast_check()
    clock.advance(timedelta(minutes=10))
    await runner._run_forecast_check()

    assert forecast_job.calls == 1  # second attempt guarded out
    assert recorder.recorded == []


# --------------------------------------------------------------------------- #
# Job registration (Req 14.1)
# --------------------------------------------------------------------------- #
async def test_register_jobs_wires_interval_and_digest_triggers() -> None:
    scheduler = _FakeScheduler()
    runner = _runner(
        forecast_job=_FakeForecastJob(),
        recorder=_FakeRecorder(),
        clock=_Clock(_START),
        scheduler=scheduler,
    )

    runner.register_jobs()

    ids = {job.id for job in scheduler.jobs}
    assert ids == {
        FORECAST_CHECK_JOB_ID,
        MORNING_DIGEST_JOB_ID,
        EVENING_DIGEST_JOB_ID,
        WEEKLY_DIGEST_JOB_ID,
    }

    # Forecast-check: interval trigger with overlap prevention (Req 14.1, 14.7).
    forecast = scheduler.by_id(FORECAST_CHECK_JOB_ID)
    assert isinstance(forecast.trigger, IntervalTrigger)
    assert forecast.kwargs["max_instances"] == 1
    assert forecast.kwargs["coalesce"] is True

    # Digests: cron triggers parsed from the configured times.
    for job_id in (MORNING_DIGEST_JOB_ID, EVENING_DIGEST_JOB_ID, WEEKLY_DIGEST_JOB_ID):
        assert isinstance(scheduler.by_id(job_id).trigger, CronTrigger)


async def test_start_registers_then_starts_scheduler() -> None:
    scheduler = _FakeScheduler()
    runner = _runner(
        forecast_job=_FakeForecastJob(),
        recorder=_FakeRecorder(),
        clock=_Clock(_START),
        scheduler=scheduler,
    )

    runner.start()

    assert scheduler.started is True
    assert len(scheduler.jobs) == 4  # registered on first start


async def test_register_jobs_rejects_malformed_digest_time() -> None:
    scheduler = _FakeScheduler()
    runner = _runner(
        forecast_job=_FakeForecastJob(),
        recorder=_FakeRecorder(),
        clock=_Clock(_START),
        scheduler=scheduler,
        settings=_settings(MORNING_DIGEST_TIME="7am"),
    )

    with pytest.raises(ValueError):
        runner.register_jobs()


async def test_register_jobs_rejects_unknown_weekday() -> None:
    runner = _runner(
        forecast_job=_FakeForecastJob(),
        recorder=_FakeRecorder(),
        clock=_Clock(_START),
        scheduler=_FakeScheduler(),
        settings=_settings(WEEKLY_DIGEST="FUNDAY 07:00"),
    )

    with pytest.raises(ValueError):
        runner.register_jobs()


# --------------------------------------------------------------------------- #
# Integration with the real shared scheduler-run state
# --------------------------------------------------------------------------- #
async def test_records_into_real_inmemory_scheduler_state() -> None:
    clock = _Clock(_START)
    state = InMemorySchedulerState()
    runner = SchedulerRunner(
        _FakeForecastJob(),  # type: ignore[arg-type]
        _digest_runner_stub(),
        state,
        _settings(),
        scheduler=_FakeScheduler(),
        now=clock,
    )

    assert await state.last_successful_run_async() is None  # "never" before any run
    await runner._run_forecast_check()
    assert await state.last_successful_run_async() == _START
