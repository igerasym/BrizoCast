"""Pure anti-spam notification decision policy (no I/O).

Decides whether an immediate alert should be sent for a freshly-computed
:class:`~brizocast.core.domain.scoring.ScoreResult`, given the most-recent prior
notification for the same ``(subscription, spot, forecast window)`` and the
configured :term:`Significant_Improvement` threshold.

The policy is a single pure function, :func:`decide`, returning a
:class:`NotificationDecision`. It performs no persistence, no Telegram dispatch,
and imports nothing from the ORM or service layers, so it is deterministic and
property-testable in isolation (supports Property 6). The
:class:`~brizocast.notifications.engine.NotificationEngine` (task 5.x) layers the
mute / snooze / quiet-hours / digest gating *around* this decision; this module
owns only the score-vs-history comparison.

Decision table (Req 9.1, 9.3, 9.4, 9.5):

==================================================  ==================
Condition                                           Decision
==================================================  ==================
candidate category below ``RIDEABLE``               ``SUPPRESS``  (9.1)
no prior record and candidate qualifies             ``SEND_NEW``  (9.2)
candidate score ≤ prior score                       ``SUPPRESS``  (9.3)
improvement over prior < ``significant_improvement``  ``SUPPRESS``  (9.4)
improvement over prior ≥ ``significant_improvement``  ``SEND_IMPROVED`` (9.5)
==================================================  ==================

The branches are evaluated top-to-bottom and are mutually exclusive once
reached in order, so the table is total over every ``(candidate, last, cfg)``
combination.

``last`` typing choice
----------------------
``last`` is typed as the structural :class:`PriorNotification` protocol — any
object exposing an ``int`` ``score`` attribute — or ``None`` when no prior
record exists. This was chosen over a bare ``int | None`` because it lets the
notification engine pass its persisted ``Notification_Record`` entity straight
through (the engine looks up ``notification_repo.latest(...)`` and forwards the
result) without unpacking a field or this pure module importing the ORM model.
It keeps the call site identical to the design pseudocode (``last.score``) while
preserving the no-persistence-import rule.

Requirements covered: 9.1, 9.3, 9.4, 9.5 (Notification anti-spam; supports
Property 6). The :term:`Significant_Improvement` threshold itself is defined in
Configuration (Req 9.6) and surfaced here through :class:`AntiSpamConfig`.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from brizocast.core.domain.scoring import ScoreCategory, ScoreResult


class NotificationDecision(Enum):
    """The outcome of the anti-spam policy for a candidate score.

    ``SEND_NEW`` and ``SEND_IMPROVED`` both authorize dispatch; they are kept
    distinct so the caller can tailor the alert (a first qualifying alert versus
    an "improved conditions" update) and so the decision table is observable in
    tests. ``SUPPRESS`` means no immediate alert for this forecast window.
    """

    SEND_NEW = "send_new"
    SEND_IMPROVED = "send_improved"
    SUPPRESS = "suppress"


@runtime_checkable
class PriorNotification(Protocol):
    """Structural view of the most-recent prior notification for a window.

    The anti-spam policy only needs the previously-notified surf score, so it
    depends on this minimal protocol rather than the persisted
    ``Notification_Record`` ORM entity. Any object exposing an integer ``score``
    satisfies it, allowing the notification engine to forward its repository
    result directly while this module stays free of persistence imports.
    """

    @property
    def score(self) -> int:
        """The surf score recorded by the prior notification (0..100)."""
        ...


class AntiSpamConfig(BaseModel):
    """Configuration inputs for the anti-spam decision policy.

    Carries the :term:`Significant_Improvement` threshold — the minimum number
    of score points by which a new score must exceed the most-recent prior
    notification to warrant an updated alert (Req 9.4, 9.5). The value is sourced
    from ``Settings.SIGNIFICANT_IMPROVEMENT`` (Req 9.6) at composition time.

    Frozen so a configured policy input cannot be mutated after construction.
    """

    model_config = ConfigDict(frozen=True)

    significant_improvement: int = Field(
        ge=0,
        description="Minimum score-point increase over the prior notification to re-alert.",
    )


def decide(
    candidate: ScoreResult,
    last: PriorNotification | None,
    cfg: AntiSpamConfig,
) -> NotificationDecision:
    """Decide whether to send an immediate alert for ``candidate``.

    Applies the anti-spam decision table in order: a candidate below the
    ``RIDEABLE`` category is always suppressed (Req 9.1); with no prior record a
    qualifying candidate is a new alert (Req 9.2 baseline); otherwise the
    candidate must both exceed the prior score (Req 9.3) and do so by at least
    the configured significant-improvement threshold (Req 9.4) to be sent as an
    updated alert (Req 9.5).

    The function is pure: it reads only its arguments and returns a
    :class:`NotificationDecision`. Gating by mute, snooze, quiet hours, and
    digest mode is the caller's responsibility.

    :param candidate: The freshly-computed score for a forecast window.
    :param last: The most-recent prior notification for the same
        ``(subscription, spot, forecast window)``, or ``None`` if none exists.
    :param cfg: The anti-spam configuration carrying the significant-improvement
        threshold.
    :returns: ``SUPPRESS``, ``SEND_NEW``, or ``SEND_IMPROVED`` per the table.
    """

    # Req 9.1 — a sub-Rideable candidate never produces an immediate alert,
    # regardless of any prior history.
    if candidate.category < ScoreCategory.RIDEABLE:
        return NotificationDecision.SUPPRESS

    # Req 9.2 (baseline) — first qualifying alert for this window.
    if last is None:
        return NotificationDecision.SEND_NEW

    # Req 9.3 — an equal or lower score is a duplicate; suppress it.
    if candidate.score <= last.score:
        return NotificationDecision.SUPPRESS

    # Req 9.4 — higher, but not by enough to be worth re-alerting.
    if candidate.score - last.score < cfg.significant_improvement:
        return NotificationDecision.SUPPRESS

    # Req 9.5 — a significant improvement warrants an updated alert.
    return NotificationDecision.SEND_IMPROVED
