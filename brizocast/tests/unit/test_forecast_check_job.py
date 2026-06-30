"""Unit tests for :class:`ForecastCheckJob` (task 8.1, supports Property 24).

Exercise the forecast-check pipeline against in-memory fakes for every
collaborator — subscription/discovery/forecast/preset services, a fake activity
+ scorer registered in the process-global :class:`ActivityRegistry`, a real
:class:`NotificationEngine` over a fake history lookup, and a real
:class:`RetryingNotificationSender` wrapping a controllable fake message sender.

Verified behaviours:

* a run selects each subscription's scorer by activity, scores its spots, and
  dispatches exactly the alerts the anti-spam/gating policies permit, recording
  a notification for each delivered alert (Req 9.2, 14.2, 17.6);
* an error while processing one subscription is isolated — the run logs it and
  continues with the remaining subscriptions (Req 14.3, Property 24);
* a forecast provider error skips only the affected spot, not the whole
  subscription (Req 6.5);
* a subscription with no nearby spots is skipped (Req 5.5);
* digest-mode qualifying scores are buffered (returned and pushed to the sink),
  and an immediate alert whose delivery exhausts its retries is deferred to the
  digest (Req 10.4);
* a muted subscription dispatches nothing (gating);
* a subscription whose owner has no resolvable chat id is skipped.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from brizocast.activities.base import Activity
from brizocast.activities.registry import ActivityRegistry
from brizocast.activities.surf.conditions import SurfConditions
from brizocast.core.domain.antispam import AntiSpamConfig
from brizocast.core.domain.conditions import ConditionsModel
from brizocast.core.domain.daylight import DaylightInfo
from brizocast.core.domain.forecast import Forecast, ForecastStep, ForecastWindow
from brizocast.core.domain.geo import GeoPoint
from brizocast.core.domain.scoring import ScoreBreakdown, ScoreCategory, ScoreResult
from brizocast.core.domain.scoring_types import FactorContribution
from brizocast.core.domain.spot import SurfSpot
from brizocast.core.errors import NotFoundError, ProviderRequestError
from brizocast.core.ports.scorer import DaylightResolver
from brizocast.models.notification import NotificationSent
from brizocast.models.subscription import Subscription
from brizocast.notifications.engine import NotificationEngine
from brizocast.notifications.modes import DigestItem
from brizocast.notifications.sender import (
    InlineKeyboard,
    RetryingNotificationSender,
)
from brizocast.scheduler.forecast_check_job import ForecastCheckJob
from brizocast.services.subscription_service import SubscriptionForecastTarget

pytestmark = pytest.mark.unit

_FAKE_ACTIVITY_KEY = "faketest"
_NOW = datetime(2025, 6, 1, 5, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fakes — scoring
# --------------------------------------------------------------------------- #
def _score_result(score: int, *, when: datetime) -> ScoreResult:
    """Build a :class:`ScoreResult` for ``score`` over a single-instant window."""
    factor = FactorContribution(value=0.0, weight=0.2)
    breakdown = ScoreBreakdown(
        wave_height=factor,
        swell_period=factor,
        wind_speed=factor,
        wind_direction=factor,
        swell_direction=factor,
        total_weighted=0.0,
    )
    return ScoreResult(
        score=score,
        category=ScoreCategory.from_score(score),
        breakdown=breakdown,
        forecast_window=ForecastWindow(start=when, end=when),
    )


class _FakeScorer:
    """Scorer that maps each step's wave height to a score (1 m = 1 point)."""

    def score(
        self, step: ForecastStep, conditions: Any, daylight: DaylightInfo
    ) -> ScoreResult:  # pragma: no cover - score_series is the used path
        return _score_result(int(step.wave_height_m), when=step.timestamp)

    def score_series(
        self,
        forecast: Forecast,
        conditions: Any,
        daylight_resolver: DaylightResolver | None = None,
    ) -> list[ScoreResult]:
        return [
            _score_result(int(step.wave_height_m), when=step.timestamp)
            for step in forecast.steps
        ]


class _FakeActivity(Activity[Any]):
    """A registered activity whose scorer is the deterministic fake scorer."""

    key = _FAKE_ACTIVITY_KEY
    display_name = "Fake"
    available_in_mvp = True

    def scorer(self) -> Any:
        return _FakeScorer()

    def conditions_schema(self) -> type[ConditionsModel]:  # pragma: no cover
        return SurfConditions

    def default_forecast_provider_key(self) -> str:  # pragma: no cover
        return "fake"


# --------------------------------------------------------------------------- #
# Fakes — services
# --------------------------------------------------------------------------- #
class _FakeSubscriptionService:
    """Returns scripted active subscriptions and their forecast targets."""

    def __init__(
        self,
        subscriptions: list[Subscription],
        targets: dict[int, SubscriptionForecastTarget],
        *,
        target_errors: frozenset[int] = frozenset(),
    ) -> None:
        self._subscriptions = subscriptions
        self._targets = targets
        self._target_errors = target_errors

    async def list_all_active(self) -> list[Subscription]:
        return self._subscriptions

    async def get_forecast_target(
        self, subscription_id: int
    ) -> SubscriptionForecastTarget | None:
        if subscription_id in self._target_errors:
            raise NotFoundError(f"boom for {subscription_id}")
        return self._targets.get(subscription_id)


class _FakeSpotDiscovery:
    """Returns spots per subscription id (empty when none configured)."""

    def __init__(self, spots_by_sub: dict[int, tuple[SurfSpot, ...]]) -> None:
        self._spots_by_sub = spots_by_sub

    def discover(
        self, center: GeoPoint, radius_km: float, *, subscription_id: int | None = None
    ) -> Any:
        from brizocast.services.spot_discovery_service import SpotDiscoveryResult

        spots = self._spots_by_sub.get(subscription_id or -1, ())
        return SpotDiscoveryResult(
            center=center,
            radius_km=radius_km,
            spots=spots,
            subscription_id=subscription_id,
        )


class _FakeForecastService:
    """One-step forecast per spot keyed by wave height; fails for some spots."""

    def __init__(
        self,
        waves: dict[str, float],
        *,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self._waves = waves
        self._failing = failing
        self.calls: list[str] = []

    async def get_forecast(self, spot: SurfSpot, window: ForecastWindow) -> Forecast:
        self.calls.append(spot.spot_key)
        if spot.spot_key in self._failing:
            raise ProviderRequestError("down", provider="fake")
        return Forecast(
            spot_id=spot.spot_key,
            steps=[
                ForecastStep(
                    timestamp=datetime(2025, 6, 1, 6, 0, tzinfo=UTC),
                    wave_height_m=self._waves[spot.spot_key],
                    swell_period_s=11.0,
                    swell_direction_deg=300.0,
                    wind_speed_kmh=8.0,
                    wind_direction_deg=90.0,
                )
            ],
        )


class _FakePresetService:
    """Returns one fixed set of effective conditions for any subscription."""

    async def resolve_effective_conditions(
        self, subscription: Subscription, *, region: str | None = None
    ) -> SurfConditions:
        return SurfConditions(
            min_wave_m=0.5, max_wave_m=300.0, min_period_s=8.0, max_wind_kmh=25.0
        )


class _FakeHistory:
    """Engine history lookup: no prior notification for any window."""

    async def latest_for_window(
        self, subscription_id: int, spot_key: str, forecast_window_key: str
    ) -> NotificationSent | None:
        return None


class _RecordingNotificationService:
    """Captures every ``record_sent`` call (no persistence)."""

    def __init__(self) -> None:
        self.records: list[tuple[int, str, int]] = []

    async def record_sent(
        self,
        subscription_id: int,
        spot_key: str,
        score_result: ScoreResult,
        *,
        sent_at: datetime | None = None,
    ) -> NotificationSent:
        self.records.append((subscription_id, spot_key, score_result.score))
        return NotificationSent(
            subscription_id=subscription_id,
            spot_key=spot_key,
            surf_score=score_result.score,
            forecast_window_key=score_result.forecast_window.key(),
            forecast_window_start=score_result.forecast_window.start,
            forecast_window_end=score_result.forecast_window.end,
            sent_at=sent_at or _NOW,
        )


class _FakeMessageSender:
    """Records sends; raises for chat ids in ``failing`` to exhaust retries."""

    def __init__(self, *, failing: frozenset[int] = frozenset()) -> None:
        self._failing = failing
        self.sent: list[tuple[int, str]] = []

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
    ) -> None:
        if chat_id in self._failing:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text))


class _RecordingDigestSink:
    """Captures items pushed to the digest buffer per subscription."""

    def __init__(self) -> None:
        self.buffered: dict[int, tuple[DigestItem, ...]] = {}

    async def buffer(
        self, subscription_id: int, items: Sequence[DigestItem]
    ) -> None:
        self.buffered[subscription_id] = tuple(items)


class _FakeChatIds:
    """Maps internal user ids to Telegram chat ids."""

    def __init__(self, mapping: dict[int, int]) -> None:
        self._mapping = mapping

    async def chat_id_for_user(self, user_id: int) -> int | None:
        return self._mapping.get(user_id)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    """Snapshot/restore the process-global registry around each test."""
    snapshot = dict(ActivityRegistry._items)
    ActivityRegistry.register(_FakeActivity())
    try:
        yield
    finally:
        ActivityRegistry._items.clear()
        ActivityRegistry._items.update(snapshot)


def _spot(spot_key: str, name: str) -> SurfSpot:
    return SurfSpot(spot_key=spot_key, name=name, lat=39.34, lon=-9.35)


def _subscription(
    sub_id: int,
    user_id: int,
    *,
    notification_mode: str = "immediate",
    muted: bool = False,
) -> Subscription:
    sub = Subscription(
        user_id=user_id,
        activity_id=1,
        location_id=1,
        search_radius_km=30.0,
        notification_mode=notification_mode,
        muted=muted,
    )
    sub.id = sub_id
    return sub


def _target(sub: Subscription, activity_key: str = _FAKE_ACTIVITY_KEY) -> SubscriptionForecastTarget:
    return SubscriptionForecastTarget(
        subscription=sub,
        activity_key=activity_key,
        center=GeoPoint(lat=39.34, lon=-9.35),
        location_label="Peniche",
        search_radius_km=30.0,
    )


def _build_job(
    *,
    subscriptions: list[Subscription],
    targets: dict[int, SubscriptionForecastTarget],
    spots_by_sub: dict[int, tuple[SurfSpot, ...]],
    waves: dict[str, float],
    chat_ids: dict[int, int],
    target_errors: frozenset[int] = frozenset(),
    failing_spots: frozenset[str] = frozenset(),
    failing_chats: frozenset[int] = frozenset(),
    digest_sink: _RecordingDigestSink | None = None,
    retry_count: int = 1,
) -> tuple[ForecastCheckJob, _RecordingNotificationService, _FakeMessageSender, _FakeForecastService]:
    notifications = _RecordingNotificationService()
    message_sender = _FakeMessageSender(failing=failing_chats)
    forecast = _FakeForecastService(waves, failing=failing_spots)
    engine = NotificationEngine(
        _FakeHistory(),
        AntiSpamConfig(significant_improvement=10),
    )
    job = ForecastCheckJob(
        _FakeSubscriptionService(subscriptions, targets, target_errors=target_errors),  # type: ignore[arg-type]
        _FakeSpotDiscovery(spots_by_sub),  # type: ignore[arg-type]
        forecast,  # type: ignore[arg-type]
        _FakePresetService(),  # type: ignore[arg-type]
        notifications,  # type: ignore[arg-type]
        engine,
        RetryingNotificationSender(message_sender, retry_count=retry_count),
        _FakeChatIds(chat_ids),
        digest_sink=digest_sink,
        now=lambda: _NOW,
    )
    return job, notifications, message_sender, forecast


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_run_scores_and_dispatches_permitted_alerts() -> None:
    sub = _subscription(1, user_id=100)
    spots = (_spot("pt/a", "Spot A"), _spot("pt/b", "Spot B"))
    job, notifications, sender, _ = _build_job(
        subscriptions=[sub],
        targets={1: _target(sub)},
        spots_by_sub={1: spots},
        waves={"pt/a": 80.0, "pt/b": 40.0},  # A qualifies (>=50), B is Ignore
        chat_ids={100: 9100},
    )

    result = await job.run_once()

    assert result.subscriptions_total == 1
    assert result.subscriptions_processed == 1
    assert result.immediate_dispatched == 1
    # Only the qualifying spot (A, score 80) was dispatched and recorded.
    assert sender.sent == [(9100, sender.sent[0][1])]
    assert notifications.records == [(1, "pt/a", 80)]


async def test_error_in_one_subscription_still_processes_the_rest() -> None:
    sub1 = _subscription(1, user_id=100)
    sub_bad = _subscription(2, user_id=200)
    sub3 = _subscription(3, user_id=300)
    job, notifications, sender, _ = _build_job(
        subscriptions=[sub1, sub_bad, sub3],
        targets={1: _target(sub1), 3: _target(sub3)},
        spots_by_sub={1: (_spot("pt/a", "A"),), 3: (_spot("pt/c", "C"),)},
        waves={"pt/a": 70.0, "pt/c": 90.0},
        chat_ids={100: 9100, 200: 9200, 300: 9300},
        target_errors=frozenset({2}),  # sub 2 raises during resolution
    )

    result = await job.run_once()

    # The bad subscription is isolated; the others still dispatch (Property 24).
    assert result.subscriptions_failed == 1
    assert result.subscriptions_processed == 2
    assert result.immediate_dispatched == 2
    dispatched_chats = sorted(chat for chat, _ in sender.sent)
    assert dispatched_chats == [9100, 9300]
    assert sorted(notifications.records) == [(1, "pt/a", 70), (3, "pt/c", 90)]


async def test_provider_error_skips_only_that_spot() -> None:
    sub = _subscription(1, user_id=100)
    spots = (_spot("pt/bad", "Bad"), _spot("pt/ok", "OK"))
    job, notifications, sender, forecast = _build_job(
        subscriptions=[sub],
        targets={1: _target(sub)},
        spots_by_sub={1: spots},
        waves={"pt/ok": 65.0},  # bad spot has no wave entry; it fails first
        chat_ids={100: 9100},
        failing_spots=frozenset({"pt/bad"}),
    )

    result = await job.run_once()

    # Both spots attempted; only the working one produced an alert (Req 6.5).
    assert forecast.calls == ["pt/bad", "pt/ok"]
    assert result.subscriptions_processed == 1
    assert result.immediate_dispatched == 1
    assert notifications.records == [(1, "pt/ok", 65)]


async def test_no_nearby_spots_is_skipped() -> None:
    sub = _subscription(1, user_id=100)
    job, notifications, sender, forecast = _build_job(
        subscriptions=[sub],
        targets={1: _target(sub)},
        spots_by_sub={1: ()},  # nothing within radius (Req 5.5)
        waves={},
        chat_ids={100: 9100},
    )

    result = await job.run_once()

    assert result.subscriptions_processed == 1
    assert result.immediate_dispatched == 0
    assert forecast.calls == []  # no forecast collection for a no-spots sub
    assert notifications.records == []


async def test_digest_mode_buffers_qualifying_scores() -> None:
    sub = _subscription(1, user_id=100, notification_mode="morning_digest")
    sink = _RecordingDigestSink()
    job, notifications, sender, _ = _build_job(
        subscriptions=[sub],
        targets={1: _target(sub)},
        spots_by_sub={1: (_spot("pt/a", "A"),)},
        waves={"pt/a": 77.0},
        chat_ids={100: 9100},
        digest_sink=sink,
    )

    result = await job.run_once()

    # Digest mode dispatches nothing immediately but buffers the score.
    assert result.immediate_dispatched == 0
    assert sender.sent == []
    assert notifications.records == []
    assert len(result.digest_buffered[1]) == 1
    assert result.digest_buffered[1][0].spot.spot_key == "pt/a"
    assert sink.buffered[1] == result.digest_buffered[1]


async def test_failed_delivery_is_deferred_to_digest() -> None:
    sub = _subscription(1, user_id=100)
    job, notifications, sender, _ = _build_job(
        subscriptions=[sub],
        targets={1: _target(sub)},
        spots_by_sub={1: (_spot("pt/a", "A"),)},
        waves={"pt/a": 88.0},
        chat_ids={100: 9100},
        failing_chats=frozenset({9100}),  # delivery always fails -> retries exhaust
    )

    result = await job.run_once()

    # Retries exhausted: nothing recorded, the alert is deferred to the digest.
    assert result.immediate_dispatched == 0
    assert result.immediate_failed == 1
    assert notifications.records == []
    assert len(result.digest_buffered[1]) == 1
    assert result.digest_buffered[1][0].spot.spot_key == "pt/a"


async def test_muted_subscription_dispatches_nothing() -> None:
    sub = _subscription(1, user_id=100, muted=True)
    job, notifications, sender, _ = _build_job(
        subscriptions=[sub],
        targets={1: _target(sub)},
        spots_by_sub={1: (_spot("pt/a", "A"),)},
        waves={"pt/a": 95.0},
        chat_ids={100: 9100},
    )

    result = await job.run_once()

    assert result.subscriptions_processed == 1
    assert result.immediate_dispatched == 0
    assert sender.sent == []
    assert notifications.records == []
    assert result.digest_buffered == {}


async def test_subscription_without_chat_id_is_skipped() -> None:
    sub = _subscription(1, user_id=100)
    job, notifications, sender, forecast = _build_job(
        subscriptions=[sub],
        targets={1: _target(sub)},
        spots_by_sub={1: (_spot("pt/a", "A"),)},
        waves={"pt/a": 80.0},
        chat_ids={},  # no chat id resolvable for user 100
    )

    result = await job.run_once()

    assert result.subscriptions_skipped == 1
    assert result.subscriptions_processed == 0
    assert forecast.calls == []
    assert sender.sent == []
