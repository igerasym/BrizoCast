"""Digest jobs — morning, evening, and weekly-best-day summaries (task 8.5).

This module implements the *job callables* the scheduler runs at the configured
digest times (Req 10.5, 10.6, 10.7). It deliberately does **not** wire the
APScheduler triggers — that is the runner's responsibility (task 8.3). Each job
is an ``async`` method on :class:`DigestJobRunner` and can be registered with a
scheduler trigger or invoked directly in tests.

What a digest job does
----------------------
For its notification mode (morning, evening, or weekly-best-day) a job:

#. Looks up the subscriptions whose ``notification_mode`` matches the digest
   mode, via the injected :class:`DigestSubscriptionSource`.
#. Drains that subscription's buffered qualifying scores for the period from the
   shared :class:`DigestBuffer`.
#. Calls :func:`brizocast.notifications.modes.build_digest`. When it returns
   ``None`` — the period buffered no qualifying scores — the job sends **nothing**
   for that subscription (Req 10.8). The weekly job's ``build_digest`` selects
   the single best forecast day for it (Req 10.7).
#. Otherwise formats one summary message with
   :func:`brizocast.bot.formatters.digests.format_digest` and enqueues it.
#. Dispatches all enqueued summaries as a single resilient batch through the
   injected sender (one message per subscription).

The digest buffer hand-off (read by tasks 8.1 and 11.1)
-------------------------------------------------------
Digest items are produced **upstream** by the notification engine: for a
digest-mode subscription, :meth:`NotificationEngine.process` returns a
:class:`~brizocast.notifications.engine.NotificationPlan` whose
``digest`` tuple holds the qualifying :class:`~brizocast.notifications.modes.DigestItem`
values for that run (and any immediate alert deferred by quiet hours, Req 11.2).

This module defines a small, typed, **injectable** :class:`DigestBuffer` as the
agreed hand-off point between the producer and these consumers:

* **Producer — the forecast-check job (task 8.1)** appends each subscription's
  ``plan.digest`` items to the buffer keyed by ``subscription_id``::

      digest_buffer.append(plan.subscription_id, plan.digest)

  The same buffer is where the immediate-delivery retry exhaustion path
  (Req 10.4) parks a failed alert so it rides along in the next digest.

* **Consumers — the digest jobs (this module)** drain a subscription's items
  when their period fires::

      items = digest_buffer.drain(subscription_id)

* **Composition root (task 11.1)** constructs a single :class:`DigestBuffer`
  instance and injects the *same* object into both the forecast-check job and
  this :class:`DigestJobRunner`, so what the producer buffers is exactly what the
  consumers drain.

The buffer is an in-memory store (acceptable for the MVP single-process
deployment). It assumes a single event loop — APScheduler's ``AsyncIOScheduler``
runs the forecast-check and digest jobs on the same loop, and neither
:meth:`DigestBuffer.append` nor :meth:`DigestBuffer.drain` awaits between reading
and mutating its state, so there is no interleaving hazard.

Requirements covered: 10.5, 10.6, 10.7 (and 10.8 — an empty period sends
nothing).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from brizocast.bot.formatters.digests import format_digest
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.notifications.modes import (
    Digest,
    DigestItem,
    DigestPeriod,
    NotificationMode,
    build_digest,
)
from brizocast.notifications.sender import RetryingNotificationSender, SendRequest, SendResult

__all__ = [
    "DigestBuffer",
    "DigestJobRunner",
    "DigestSubscriptionSource",
    "DigestTarget",
]


# --------------------------------------------------------------------------- #
# Shared digest buffer (producer/consumer hand-off)
# --------------------------------------------------------------------------- #
class DigestBuffer:
    """In-memory, per-subscription buffer of qualifying :class:`DigestItem`s.

    The agreed hand-off between the forecast-check job (producer, task 8.1) and
    the digest jobs (consumers, this module). The producer :meth:`append`s a
    subscription's buffered scores as they are detected; the matching digest job
    :meth:`drain`s them when its period fires.

    Items are stored per ``subscription_id`` in arrival order. The store is
    intended for use on a single event loop (see the module docstring); its
    methods never await, so a read-modify-write is atomic with respect to other
    coroutines.
    """

    def __init__(self) -> None:
        """Create an empty buffer."""

        self._items: dict[int, list[DigestItem]] = defaultdict(list)

    def append(self, subscription_id: int, items: Iterable[DigestItem]) -> None:
        """Buffer ``items`` for ``subscription_id`` (called by the producer).

        Appends in arrival order. Passing an empty iterable is a no-op and never
        creates an entry, so :meth:`pending_subscriptions` reflects only
        subscriptions that actually have buffered scores.

        :param subscription_id: The subscription the scores belong to.
        :param items: The qualifying digest items to buffer (e.g. a notification
            plan's ``digest`` tuple).
        """

        new_items = list(items)
        if not new_items:
            return
        self._items[subscription_id].extend(new_items)

    def drain(self, subscription_id: int) -> list[DigestItem]:
        """Remove and return all buffered items for ``subscription_id``.

        Returns the items in arrival order and clears the subscription's buffer,
        so a subsequent drain (before any new append) returns an empty list.
        Draining a subscription with nothing buffered returns ``[]``.

        :param subscription_id: The subscription whose buffer to drain.
        :returns: The buffered items in arrival order (possibly empty).
        """

        return self._items.pop(subscription_id, [])

    def peek(self, subscription_id: int) -> list[DigestItem]:
        """Return a copy of the buffered items without draining them.

        Useful for diagnostics and tests; the live buffer is left untouched.
        """

        return list(self._items.get(subscription_id, ()))

    def pending_subscriptions(self) -> list[int]:
        """Return the ids of subscriptions that currently have buffered items."""

        return [sub_id for sub_id, items in self._items.items() if items]


# --------------------------------------------------------------------------- #
# Collaborator ports (keep the runner free of ORM/service imports)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DigestTarget:
    """A subscription a digest should be delivered to.

    Pairs the ``subscription_id`` used as the :class:`DigestBuffer` key with the
    Telegram ``chat_id`` the summary is sent to. The composition root (task
    11.1) resolves these by joining each digest-mode subscription with its owning
    user's Telegram id.
    """

    subscription_id: int
    chat_id: int


@runtime_checkable
class DigestSubscriptionSource(Protocol):
    """Resolves the delivery targets for a digest mode.

    Satisfied at the composition root by an adapter over the subscription and
    user services that returns one :class:`DigestTarget` per subscription whose
    ``notification_mode`` matches ``mode``; tests supply an in-memory fake.
    """

    async def targets_for_mode(self, mode: NotificationMode) -> Sequence[DigestTarget]:
        """Return the delivery targets for subscriptions in ``mode``."""
        ...


@runtime_checkable
class Clock(Protocol):
    """A nullary clock returning the current UTC time (injected for tests)."""

    def __call__(self) -> datetime:
        """Return the current time."""
        ...


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""

    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Digest job runner
# --------------------------------------------------------------------------- #
class DigestJobRunner:
    """The morning, evening, and weekly-best-day digest job callables.

    Constructed with the collaborators it needs and exposes three ``async``
    methods — :meth:`run_morning_digest`, :meth:`run_evening_digest`, and
    :meth:`run_weekly_digest` — each suitable for registration as an APScheduler
    job (task 8.3 wires the triggers) or for direct invocation in tests. All
    three delegate to the shared :meth:`_run` so the per-mode behaviour stays
    identical apart from the digest mode and its selection rule.
    """

    def __init__(
        self,
        *,
        buffer: DigestBuffer,
        subscriptions: DigestSubscriptionSource,
        sender: RetryingNotificationSender,
        now: Clock | None = None,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the runner.

        Args:
            buffer: The shared digest buffer the forecast-check job appends to
                and these jobs drain (the producer/consumer hand-off).
            subscriptions: Resolves the delivery targets for a digest mode.
            sender: Resilient sender used to dispatch the summaries as a batch;
                a failure for one subscription does not abort the rest
                (Req 18.3).
            now: Clock returning the current UTC time, injected for
                deterministic periods in tests; defaults to the system clock.
            logger: Optional bound logger; one is created when omitted.
        """

        self._buffer = buffer
        self._subscriptions = subscriptions
        self._sender = sender
        self._now = now or _utc_now
        self._log = logger or get_logger(__name__)

    async def run_morning_digest(self) -> list[SendResult]:
        """Run the morning-digest job (Req 10.5)."""

        return await self._run(NotificationMode.MORNING_DIGEST)

    async def run_evening_digest(self) -> list[SendResult]:
        """Run the evening-digest job (Req 10.6)."""

        return await self._run(NotificationMode.EVENING_DIGEST)

    async def run_weekly_digest(self) -> list[SendResult]:
        """Run the weekly-best-day digest job (Req 10.7)."""

        return await self._run(NotificationMode.WEEKLY_BEST_DAY)

    async def _run(self, mode: NotificationMode) -> list[SendResult]:
        """Build and dispatch one summary per ``mode`` subscription with items.

        For each delivery target in ``mode`` it drains the subscription's
        buffered scores, builds the digest (which selects the weekly best day
        when applicable, Req 10.7), and — only when there is something to send
        (Req 10.8) — formats and enqueues one summary message. All enqueued
        summaries are then dispatched as a single resilient batch.

        :param mode: The digest mode to run; never
            :attr:`NotificationMode.IMMEDIATE`.
        :returns: The per-message :class:`SendResult` outcomes (empty when no
            subscription had qualifying scores).
        """

        log = self._log.bind(digest_mode=mode.value)
        targets = await self._subscriptions.targets_for_mode(mode)
        now = self._now()

        requests: list[SendRequest] = []
        skipped = 0
        for target in targets:
            items = self._buffer.drain(target.subscription_id)
            digest = build_digest(mode, items, _period_for(items, now))
            if digest is None:
                # Empty period (no qualifying scores) -> send nothing (Req 10.8).
                skipped += 1
                continue
            requests.append(
                SendRequest(
                    chat_id=target.chat_id,
                    text=format_digest(digest),
                    ref=target.subscription_id,
                )
            )

        log.info(
            "digest run prepared: %d to send, %d skipped (no qualifying scores)",
            len(requests),
            skipped,
        )
        if not requests:
            return []
        return await self._sender.send_batch(requests)


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as a UTC-aware datetime (naive values treated as UTC)."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _period_for(items: Sequence[DigestItem], now: datetime) -> DigestPeriod:
    """Derive the :class:`DigestPeriod` metadata for a drained buffer.

    The period is carried on the produced digest purely as header metadata
    (selection does not filter by it). It spans from the earliest buffered
    item's forecast window to ``now``; an empty buffer collapses to ``[now,
    now]`` (the resulting digest is ``None`` and never rendered anyway).

    Timestamps are normalized to UTC so the comparisons never mix naive and
    aware datetimes.
    """

    now_utc = _as_utc(now)
    if not items:
        return DigestPeriod(start=now_utc, end=now_utc)
    timestamps = [_as_utc(item.timestamp) for item in items]
    start = min(timestamps)
    end = max(now_utc, max(timestamps))
    return DigestPeriod(start=start, end=end)
