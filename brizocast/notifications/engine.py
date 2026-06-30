"""Notification engine — gating, anti-spam, and mode routing (task 5.1).

The :class:`NotificationEngine` turns a subscription's freshly-scored forecast
candidates into a :class:`NotificationPlan`: the alerts to dispatch *now* and the
items to buffer for the subscription's next digest. It composes three concerns
the design keeps separate:

#. **Gating** (Req 11.2-11.6, supports Property 8) — mute and snooze suppress
   *all* notifications; quiet hours suppress *immediate* alerts only.
#. **Anti-spam** (Req 9.x) — the pure
   :func:`brizocast.core.domain.antispam.decide` policy compares each candidate
   against the most-recent notification for the same
   ``(subscription, spot, forecast window)`` and may suppress it.
#. **Mode routing** (Req 10.3) — qualifying immediate-mode candidates become
   :class:`ImmediateDispatch` items (or, during quiet hours, are deferred to the
   digest queue); digest-mode candidates are buffered as
   :class:`~brizocast.notifications.modes.DigestItem`.

Separation of decision and delivery
------------------------------------
The engine is **decision logic only**. It never touches Telegram: it does not
import or call the :mod:`brizocast.notifications.sender`. Its only collaborator
with side effects is a read-only history lookup (injected as the
:class:`WindowHistoryLookup` port, satisfied by
:class:`~brizocast.services.notification_service.NotificationService`). The
caller (the scheduler's forecast-check job, task 8.1) performs the actual send
through the sender and, **on success**, persists the
``Notification_Record`` via ``NotificationService.record_sent`` (Req 9.2). This
keeps the engine deterministic and unit-testable with fakes — no real Telegram,
no real database.

Gating semantics (Property 8)
-----------------------------
Evaluated in order for a subscription at ``now``:

* **muted** → suppress everything (Req 11.3); because the mute check is first,
  notifications stay suppressed after a snooze elapses while still muted
  (Req 11.6).
* **snoozed** (``now < snooze_until``) → suppress everything (Req 11.4),
  including when muted (the mute check already covered that case).
* otherwise notifications **resume** (Req 11.5); for immediate mode they are
  additionally suppressed (deferred to the next digest) while ``now`` falls
  within the subscription's quiet hours (Req 11.2, 10.3). Quiet hours never
  affect digest modes.

Requirements covered: 9.2, 10.3, 11.2, 11.3, 11.4, 11.5, 11.6 (supports
Property 8).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Protocol, runtime_checkable

from brizocast.core.domain.antispam import (
    AntiSpamConfig,
    NotificationDecision,
    decide,
)
from brizocast.core.domain.scoring import ScoreCategory, ScoreResult
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.notifications.modes import DigestItem, NotificationMode
from brizocast.notifications.window import window_key

__all__ = [
    "GatedSubscription",
    "ImmediateDispatch",
    "NotificationEngine",
    "NotificationPlan",
    "SentRecord",
    "WindowHistoryLookup",
    "in_quiet_hours",
    "is_gated",
]


# --------------------------------------------------------------------------- #
# Collaborator ports (structural — keep the engine free of ORM/service imports)
# --------------------------------------------------------------------------- #
@runtime_checkable
class GatedSubscription(Protocol):
    """The subscription fields the engine needs to gate and route alerts.

    A structural view rather than the SQLAlchemy
    :class:`~brizocast.models.subscription.Subscription` entity, so the engine
    stays free of persistence imports and can be exercised with a plain test
    double. The ORM model exposes exactly these attributes, so it satisfies the
    protocol at the composition root.
    """

    @property
    def id(self) -> int:
        """The subscription's primary key (used as the dedup identity)."""
        ...

    @property
    def notification_mode(self) -> str:
        """The persisted notification-mode key (see :class:`NotificationMode`)."""
        ...

    @property
    def muted(self) -> bool:
        """Whether the subscription is muted (suppresses all notifications)."""
        ...

    @property
    def snooze_until(self) -> datetime | None:
        """Instant the snooze expires, or ``None`` when not snoozed."""
        ...

    @property
    def quiet_hours_start(self) -> time | None:
        """Inclusive start of daily quiet hours, or ``None`` when unset."""
        ...

    @property
    def quiet_hours_end(self) -> time | None:
        """Exclusive end of daily quiet hours, or ``None`` when unset."""
        ...


@runtime_checkable
class SentRecord(Protocol):
    """The single field the anti-spam policy needs from a prior notification.

    Satisfied by the persisted
    :class:`~brizocast.models.notification.NotificationSent` (it exposes
    ``surf_score``), so the engine can forward a repository result without
    importing the ORM model.
    """

    @property
    def surf_score(self) -> int:
        """The surf score recorded by the prior notification (0..100)."""
        ...


@runtime_checkable
class WindowHistoryLookup(Protocol):
    """Read-only lookup of the latest notification for a dedup identity.

    Matches
    :meth:`brizocast.services.notification_service.NotificationService.latest_for_window`
    so the real service is injectable directly; tests supply an in-memory fake.
    """

    async def latest_for_window(
        self,
        subscription_id: int,
        spot_key: str,
        forecast_window_key: str,
    ) -> SentRecord | None:
        """Return the most recent record for the identity, or ``None``."""
        ...


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _PriorScore:
    """Adapts a :class:`SentRecord`'s ``surf_score`` to the anti-spam policy.

    The pure :func:`brizocast.core.domain.antispam.decide` reads ``.score`` from
    its ``last`` argument; a persisted record exposes ``surf_score``. This tiny
    frozen value bridges the two without the policy importing the ORM.
    """

    score: int


@dataclass(frozen=True)
class ImmediateDispatch:
    """A qualifying immediate-mode alert the caller should send now.

    Carries everything the caller needs to deliver and then record the alert:
    the target ``chat_id``, the originating ``subscription_id``, the scored
    ``item`` (surf spot + :class:`ScoreResult`), and the anti-spam ``decision``
    (``SEND_NEW`` or ``SEND_IMPROVED``) so the formatter can tailor the wording.
    """

    subscription_id: int
    chat_id: int
    item: DigestItem
    decision: NotificationDecision

    @property
    def spot_key(self) -> str:
        """The surf spot key the alert concerns."""
        return self.item.spot.spot_key

    @property
    def score_result(self) -> ScoreResult:
        """The scored result that will be dispatched and recorded."""
        return self.item.score_result


@dataclass(frozen=True)
class NotificationPlan:
    """The engine's decision for one subscription at one moment.

    Attributes:
        subscription_id: The subscription the plan was computed for.
        chat_id: The Telegram chat the alerts target.
        immediate: Alerts to dispatch now (empty when gated, in a digest mode,
            or fully suppressed by anti-spam / quiet hours).
        digest: Items buffered for the subscription's next digest — both
            digest-mode qualifying scores and immediate-mode alerts deferred
            because ``now`` fell within quiet hours (Req 11.2, 10.3).
        gated: ``True`` when mute or snooze suppressed *all* notifications, in
            which case ``immediate`` and ``digest`` are both empty (Req 11.3,
            11.4).
    """

    subscription_id: int
    chat_id: int
    immediate: tuple[ImmediateDispatch, ...]
    digest: tuple[DigestItem, ...]
    gated: bool

    @property
    def is_empty(self) -> bool:
        """Whether the plan dispatches and buffers nothing."""
        return not self.immediate and not self.digest


# --------------------------------------------------------------------------- #
# Pure gating helpers (clear, testable units)
# --------------------------------------------------------------------------- #
def _as_aware(moment: datetime) -> datetime:
    """Return ``moment`` as a UTC-aware datetime (naive values treated as UTC).

    Lets snooze comparisons mix tz-aware ORM timestamps and naive test values
    without raising, while preserving any timezone already present.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def in_quiet_hours(start: time | None, end: time | None, moment: datetime) -> bool:
    """Whether ``moment``'s wall-clock time falls within ``[start, end)``.

    Quiet hours are a daily ``[start, end)`` half-open window compared against
    the time-of-day of ``moment`` (in whatever timezone ``moment`` carries; the
    caller is responsible for passing the subscription's local ``now``).

    Handles windows that wrap midnight: when ``start > end`` the window spans the
    midnight boundary, so a moment is inside it when its time is at or after
    ``start`` *or* before ``end``.

    Args:
        start: Inclusive start of quiet hours, or ``None``.
        end: Exclusive end of quiet hours, or ``None``.
        moment: The instant to test.

    Returns:
        ``True`` when quiet hours are configured and ``moment`` falls inside the
        window; ``False`` when either bound is unset or the window is empty
        (``start == end``).
    """
    if start is None or end is None:
        return False
    if start == end:
        # A zero-length window is treated as "no quiet hours" rather than
        # "all day", so an accidental equal pair never silences a subscription.
        return False

    now_t = moment.timetz() if moment.tzinfo is not None else moment.time()
    # Compare on the wall-clock time component only.
    current = now_t.replace(tzinfo=None) if now_t.tzinfo is not None else now_t

    if start < end:
        return start <= current < end
    # Wraps midnight (e.g. 22:00 -> 06:00).
    return current >= start or current < end


def is_gated(subscription: GatedSubscription, now: datetime) -> bool:
    """Whether mute or snooze suppresses *all* notifications at ``now``.

    Mute is checked first so that, while muted, notifications remain suppressed
    even after the snooze elapses (Req 11.3, 11.6). An active snooze
    (``now < snooze_until``) suppresses notifications regardless of mute state
    (Req 11.4). When neither applies, notifications resume (Req 11.5) — quiet
    hours are handled separately and only affect immediate alerts.

    Args:
        subscription: The subscription whose gating state to evaluate.
        now: The current instant.

    Returns:
        ``True`` when muted or currently snoozed; ``False`` otherwise.
    """
    if subscription.muted:
        return True
    snooze_until = subscription.snooze_until
    if snooze_until is not None and _as_aware(now) < _as_aware(snooze_until):
        return True
    return False


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class NotificationEngine:
    """Decides which alerts to dispatch now and which to buffer for a digest.

    The engine is constructed with a read-only :class:`WindowHistoryLookup` and
    the :class:`~brizocast.core.domain.antispam.AntiSpamConfig`; it performs no
    delivery and no record persistence (those are the caller's responsibility
    after a successful send — Req 9.2).
    """

    def __init__(
        self,
        history: WindowHistoryLookup,
        antispam_config: AntiSpamConfig,
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the engine.

        Args:
            history: Read-only lookup of the latest notification per dedup
                identity (typically
                :class:`~brizocast.services.notification_service.NotificationService`).
            antispam_config: The significant-improvement configuration applied
                by the anti-spam policy.
            logger: Optional bound logger; one is created when omitted.
        """
        self._history = history
        self._cfg = antispam_config
        self._log = logger or get_logger(__name__)

    async def process(
        self,
        subscription: GatedSubscription,
        candidates: Sequence[DigestItem],
        now: datetime,
        *,
        chat_id: int,
    ) -> NotificationPlan:
        """Compute the notification plan for ``subscription`` at ``now``.

        Applies, in order: whole-subscription gating (mute/snooze), the anti-spam
        policy per candidate, and mode routing (immediate dispatch vs digest
        buffering, with quiet-hours deferral for immediate mode).

        Args:
            subscription: The subscription being evaluated; supplies the gating
                state, notification mode, and dedup identity (``id``).
            candidates: The best scored candidate per spot/window for this
                subscription — each pairs a surf spot with its
                :class:`ScoreResult`.
            now: The current instant, used for snooze and quiet-hours checks.
            chat_id: The Telegram chat the subscription's alerts target.

        Returns:
            A :class:`NotificationPlan` with the immediate dispatches and the
            digest-buffered items. When the subscription is muted or snoozed the
            plan is gated and empty.
        """
        log = self._log.bind(subscription_id=subscription.id)

        # 1. Whole-subscription gating: mute/snooze suppress everything
        #    (Req 11.3, 11.4, 11.6).
        if is_gated(subscription, now):
            log.info("notifications gated (muted or snoozed); suppressing all")
            return NotificationPlan(
                subscription_id=subscription.id,
                chat_id=chat_id,
                immediate=(),
                digest=(),
                gated=True,
            )

        mode = NotificationMode.from_key(subscription.notification_mode)
        quiet = in_quiet_hours(
            subscription.quiet_hours_start, subscription.quiet_hours_end, now
        )

        immediate: list[ImmediateDispatch] = []
        digest: list[DigestItem] = []

        for item in candidates:
            score_result = item.score_result

            # Req 9.1 — a sub-Rideable candidate never alerts; skip the history
            # lookup since decide() would suppress it unconditionally.
            if score_result.category < ScoreCategory.RIDEABLE:
                continue

            # 2. Anti-spam: compare against the latest record for this window.
            window = score_result.forecast_window
            last = await self._history.latest_for_window(
                subscription.id, item.spot.spot_key, window_key(window)
            )
            prior = _PriorScore(score=last.surf_score) if last is not None else None
            decision = decide(score_result, prior, self._cfg)
            if decision is NotificationDecision.SUPPRESS:
                continue

            # 3. Mode routing.
            if mode is NotificationMode.IMMEDIATE:
                if quiet:
                    # Immediate alert during quiet hours -> defer to next digest
                    # rather than dispatching now (Req 11.2, 10.3).
                    digest.append(item)
                else:
                    immediate.append(
                        ImmediateDispatch(
                            subscription_id=subscription.id,
                            chat_id=chat_id,
                            item=item,
                            decision=decision,
                        )
                    )
            else:
                # Digest mode: buffer the qualifying score for the period's
                # digest (Req 10.5-10.7); the scheduler's digest jobs send it.
                digest.append(item)

        log.info(
            "notification plan: %d immediate, %d buffered (mode=%s, quiet_hours=%s)",
            len(immediate),
            len(digest),
            mode.value,
            quiet,
        )
        return NotificationPlan(
            subscription_id=subscription.id,
            chat_id=chat_id,
            immediate=tuple(immediate),
            digest=tuple(digest),
            gated=False,
        )
