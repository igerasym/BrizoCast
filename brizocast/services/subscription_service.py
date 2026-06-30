"""Subscription management service (Req 3.*, 16.6).

``SubscriptionService`` is the application-layer use case for creating, listing,
removing, and editing a user's surf :class:`~brizocast.models.subscription.Subscription`
records. Each subscription binds **exactly one** user, activity, and location
(Req 3.1, 3.4, 16.6) together with a search radius, an optional preset, and a
notification mode.

The service owns the unit-of-work boundary: every method opens a single
``session_scope`` against the injected ``async_sessionmaker`` and drives a
:class:`~brizocast.repositories.subscription_repo.SqlAlchemySubscriptionRepository`
within it. Because the session factory is built with ``expire_on_commit=False``,
the persisted entity returned by :meth:`create` stays readable after the
transaction commits.

Validation rules enforced here (rather than in the thin bot handlers):

* The search radius must lie in ``[1, 200]`` km inclusive; out-of-range values
  raise :class:`~brizocast.core.errors.DomainValidationError` (Req 3.9, 3.10).
* When a radius is not provided it defaults to
  :data:`~brizocast.models.subscription.DEFAULT_SEARCH_RADIUS_KM` (30 km)
  (Req 3.2).
* A location is required before a subscription can be created; a missing
  location raises :class:`~brizocast.core.errors.DomainValidationError`
  (Req 3.8).

Monetization extension point
----------------------------
The monetization quota gate (``EntitlementService``, task 10.1) plugs into
:meth:`create` through the optional, injected ``on_before_create`` hook — a
single, clearly-marked call site invoked with the owning ``user_id`` *before*
the subscription is persisted. When monetization is enabled the hook raises
:class:`~brizocast.core.errors.QuotaExceededError`; when disabled (or unset) the
hook is absent and creation proceeds unchanged. This keeps the MVP creation flow
untouched while letting task 10.1 add the gate without reworking this service.

Requirements covered: 3.1, 3.2, 3.3, 3.4, 3.6, 3.7, 3.8, 3.9, 3.10, 16.6.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.config.settings import ALL_NOTIFICATION_MODES, NOTIFICATION_MODE_IMMEDIATE
from brizocast.core.domain.geo import GeoPoint
from brizocast.core.errors import DomainValidationError, NotFoundError
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.database.session import session_scope
from brizocast.models.activity import Activity
from brizocast.models.location import Location
from brizocast.models.subscription import DEFAULT_SEARCH_RADIUS_KM, Subscription
from brizocast.models.user import User
from brizocast.repositories.subscription_repo import SqlAlchemySubscriptionRepository

__all__ = [
    "MAX_SEARCH_RADIUS_KM",
    "MIN_SEARCH_RADIUS_KM",
    "AdminSubscriptionSummary",
    "BeforeCreateHook",
    "SubscriptionForecastTarget",
    "SubscriptionSummary",
    "SubscriptionService",
]

# Accepted search-radius bounds in kilometres, inclusive (Req 3.9).
MIN_SEARCH_RADIUS_KM: Final[float] = 1.0
MAX_SEARCH_RADIUS_KM: Final[float] = 200.0

# Signature of the monetization extension point (task 10.1). Invoked with the
# owning user's id before a subscription is persisted; it raises
# ``QuotaExceededError`` when the user's plan quota would be exceeded and returns
# normally otherwise.
BeforeCreateHook = Callable[[int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SubscriptionSummary:
    """Pure, presentation-ready view of one subscription (Req 3.5).

    Carries everything the ``/subscriptions`` formatter (task 7.1) needs to
    render a line for a subscription without touching ORM rows or opening a
    session: the bound activity (stable key plus human display name), the
    location (a short label plus a longer place string), the search radius in
    km, and the notification-mode key.

    Being frozen and slot-based makes it an immutable value object that is cheap
    to construct and safe to pass across layers (Req 3.5, supports Property 19).
    """

    subscription_id: int
    activity_key: str
    activity_display_name: str
    location_label: str
    location_place: str
    search_radius_km: float
    notification_mode: str


@dataclass(frozen=True, slots=True)
class AdminSubscriptionSummary:
    """Presentation-ready view of one subscription across all users (Req 3.1).

    Carries everything the admin panel's ``/subscriptions`` list (task 5.4)
    needs to render a row without touching ORM rows or opening a session: the
    **owning user's** Telegram identifier (and optional username), the bound
    activity (stable key plus human display name), the location (a short label
    plus a longer place string), the search radius in km, and the
    notification-mode key.

    Being frozen and slot-based makes it an immutable value object that is cheap
    to construct and safe to pass across layers (Req 3.1).
    """

    subscription_id: int
    owner_telegram_user_id: int
    owner_username: str | None
    activity_key: str
    activity_display_name: str
    location_label: str
    location_place: str
    search_radius_km: float
    notification_mode: str


@dataclass(frozen=True, slots=True)
class SubscriptionForecastTarget:
    """Everything the on-demand forecast pipeline needs for one subscription.

    Resolved in a single unit of work by
    :meth:`SubscriptionService.get_forecast_target` so a caller (the
    :class:`~brizocast.services.status_service.StatusService` ``/forecast`` flow,
    task 7.7) can run discovery → forecast → scoring without opening its own
    session or touching ORM relationships:

    * ``subscription`` — the (detached) subscription row; only its loaded scalar
      attributes (``id``, ``preset_id``) are read downstream, e.g. by
      :meth:`~brizocast.services.preset_service.PresetService.resolve_effective_conditions`,
      so a detached instance is safe.
    * ``activity_key`` — the activity registry key, used to resolve the scorer
      via :meth:`~brizocast.activities.registry.ActivityRegistry.get`.
    * ``center`` / ``search_radius_km`` — the discovery query for nearby spots.
    * ``location_label`` — a short user-facing label for the result message.
    """

    subscription: Subscription
    activity_key: str
    center: GeoPoint
    location_label: str
    search_radius_km: float


def _location_label(location: Location) -> str:
    """Return a short, user-facing label for ``location``.

    Prefers the user's explicit label, then the city, falling back to the
    rounded coordinates so a line is always non-empty.
    """
    if location.label:
        return location.label
    if location.city:
        return location.city
    return f"{location.lat:.4f}, {location.lon:.4f}"

def _location_place(location: Location) -> str:
    """Return a longer place string ("City, Country") for ``location``.

    Joins whichever of city and country are present; falls back to the rounded
    coordinates when neither is known (e.g. a shared Telegram point).
    """
    parts = [part for part in (location.city, location.country) if part]
    if parts:
        return ", ".join(parts)
    return f"{location.lat:.4f}, {location.lon:.4f}"


def _validate_radius(radius_km: float) -> None:
    """Raise :class:`DomainValidationError` if ``radius_km`` is out of range.

    Enforces the accepted ``[1, 200]`` km range, inclusive (Req 3.9). The bot
    surfaces the message and re-requests a valid value (Req 3.10).
    """
    if not MIN_SEARCH_RADIUS_KM <= radius_km <= MAX_SEARCH_RADIUS_KM:
        raise DomainValidationError(
            f"search radius must be between {MIN_SEARCH_RADIUS_KM:g} and "
            f"{MAX_SEARCH_RADIUS_KM:g} km (got {radius_km:g})"
        )


class SubscriptionService:
    """Create, list, remove, and edit user subscriptions (Req 3.*)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        on_before_create: BeforeCreateHook | None = None,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            session_factory: Async session maker providing the unit-of-work
                boundary; each method runs inside its own ``session_scope``.
            on_before_create: Optional monetization gate (task 10.1) invoked
                with the owning ``user_id`` before a subscription is persisted.
                Left ``None`` in the MVP so creation is ungated.
            logger: Optional bound logger; one is created when omitted.
        """
        self._session_factory = session_factory
        self._on_before_create = on_before_create
        self._log = logger or get_logger(__name__)

    async def create(
        self,
        user_id: int,
        activity_id: int,
        location_id: int | None,
        *,
        search_radius_km: float | None = None,
        preset_id: int | None = None,
        notification_mode: str = NOTIFICATION_MODE_IMMEDIATE,
    ) -> Subscription:
        """Create and persist a subscription for ``user_id`` (Req 3.1, 3.4).

        Binds exactly one user, activity, and location (Req 16.6). The radius
        defaults to 30 km when not provided (Req 3.2) and must fall in the
        accepted ``[1, 200]`` km range (Req 3.9, 3.10). A location is required
        (Req 3.8).

        Args:
            user_id: The owning user's id.
            activity_id: The bound activity's id.
            location_id: The bound location's id; ``None`` is rejected (Req 3.8).
            search_radius_km: Search radius in km; ``None`` defaults to 30 km
                (Req 3.2).
            preset_id: Optional preset to associate with the subscription.
            notification_mode: Notification mode key; defaults to immediate.

        Returns:
            The persisted :class:`Subscription` with its assigned id.

        Raises:
            DomainValidationError: If no location is supplied (Req 3.8) or the
                radius is outside the accepted range (Req 3.9, 3.10).
            QuotaExceededError: If the injected monetization gate rejects the
                creation (task 10.1; never raised while the hook is unset).
        """
        # Req 3.8 — a subscription cannot be completed without a location.
        if location_id is None:
            raise DomainValidationError(
                "a location is required before creating a subscription"
            )

        # Req 3.2 — default the radius to 30 km when the user did not specify one.
        radius = DEFAULT_SEARCH_RADIUS_KM if search_radius_km is None else search_radius_km
        # Req 3.9, 3.10 — reject radii outside [1, 200] km before persisting.
        _validate_radius(radius)

        # --- Monetization extension point (task 10.1) -------------------- #
        # The single, clearly-marked gate call. In the MVP the hook is unset and
        # this is a no-op; the EntitlementService guard raises QuotaExceededError
        # here when a user's plan quota would be exceeded — no rework required.
        if self._on_before_create is not None:
            await self._on_before_create(user_id)

        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            stored = await repo.add(
                Subscription(
                    user_id=user_id,
                    activity_id=activity_id,
                    location_id=location_id,
                    search_radius_km=radius,
                    preset_id=preset_id,
                    notification_mode=notification_mode,
                )
            )
            self._log.info(
                "created subscription %s for user %s (activity=%s, location=%s, radius=%.1f km)",
                stored.id,
                user_id,
                activity_id,
                location_id,
                radius,
            )
            return stored

    async def list_for_user(self, user_id: int) -> list[Subscription]:
        """Return every subscription owned by ``user_id`` (Req 3.3, 3.5).

        Returns the raw ORM rows. For a presentation-ready, session-free view
        (activity display, location label/place, radius, mode) use
        :meth:`summarize_for_user`, which the ``/subscriptions`` formatter
        (task 7.1) consumes.
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            return await repo.list_for_user(user_id)

    async def list_all_active(self) -> list[Subscription]:
        """Return every active subscription across all users (Req 14.2).

        Loads the raw ORM rows the scheduler's forecast-check job (task 8.1)
        iterates over: every subscription whose ``active`` flag is set, in stable
        id order. The rows are detached after the unit of work closes, but
        ``expire_on_commit=False`` keeps their scalar attributes (id, user_id,
        notification preferences) readable — exactly what the job needs to gate
        and route a subscription. For the discovery center, activity, and
        location label use :meth:`get_forecast_target` per subscription.
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            return await repo.list_all_active()

    async def summarize_all(self) -> list[AdminSubscriptionSummary]:
        """Return a presentation-ready summary for every subscription (Req 3.1).

        Produces one :class:`AdminSubscriptionSummary` for **each** active
        subscription across all users, in stable id order, resolving the owning
        user's Telegram identifier, the bound activity's display name, and the
        location's label/place within a single ``session_scope`` so the caller
        (the admin panel's ``/subscriptions`` list, task 5.4) never has to touch
        ORM rows or open its own session. The output is pure data — no HTML
        rendering happens here.

        Reuses the repository's ``list_all_active`` query rather than
        duplicating subscription-selection logic.

        Returns:
            One summary per subscription, each carrying the owner's Telegram id
            and username, the activity (key and display name), the location
            label and place, the search radius in km, and the notification mode.
            Empty when no subscriptions exist.

        Raises:
            NotFoundError: If a subscription references a user, activity, or
                location row that no longer exists (a referential-integrity
                violation).
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            subscriptions = await repo.list_all_active()

            summaries: list[AdminSubscriptionSummary] = []
            for sub in subscriptions:
                # Per-row lookups within the one unit of work; SQLAlchemy's
                # identity map serves repeated users/activities/locations from
                # cache.
                owner = await session.get(User, sub.user_id)
                if owner is None:
                    raise NotFoundError(
                        f"subscription {sub.id} references missing user "
                        f"{sub.user_id}"
                    )
                activity = await session.get(Activity, sub.activity_id)
                if activity is None:
                    raise NotFoundError(
                        f"subscription {sub.id} references missing activity "
                        f"{sub.activity_id}"
                    )
                location = await session.get(Location, sub.location_id)
                if location is None:
                    raise NotFoundError(
                        f"subscription {sub.id} references missing location "
                        f"{sub.location_id}"
                    )
                summaries.append(
                    AdminSubscriptionSummary(
                        subscription_id=sub.id,
                        owner_telegram_user_id=owner.telegram_user_id,
                        owner_username=owner.username,
                        activity_key=activity.key,
                        activity_display_name=activity.display_name,
                        location_label=_location_label(location),
                        location_place=_location_place(location),
                        search_radius_km=sub.search_radius_km,
                        notification_mode=sub.notification_mode,
                    )
                )
            return summaries

    async def get(self, subscription_id: int) -> Subscription | None:
        """Return the subscription with id ``subscription_id``, or ``None``.

        Returns the raw ORM row with its scalar attributes loaded. Because the
        session factory is built with ``expire_on_commit=False`` the returned
        instance stays readable after the unit of work closes, but its lazy
        relationships are not loaded — callers needing the bound location or
        activity should use :meth:`get_forecast_target` instead.
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            return await repo.get(subscription_id)

    async def get_forecast_target(
        self, subscription_id: int
    ) -> SubscriptionForecastTarget | None:
        """Resolve the forecast-pipeline target for one subscription (Req 13.4).

        Loads the subscription together with its bound activity and location in a
        single unit of work and projects them into a session-free
        :class:`SubscriptionForecastTarget` carrying the discovery center and
        radius, the activity registry key, the location label, and the
        subscription row itself. This is what the ``/forecast`` flow (task 7.7)
        needs to discover nearby spots, fetch forecasts, resolve effective
        conditions, and score — without opening its own session or relying on
        lazy relationships.

        Args:
            subscription_id: The subscription to resolve.

        Returns:
            The resolved target, or ``None`` if no subscription with that id
            exists.

        Raises:
            NotFoundError: If the subscription references an activity or location
                row that no longer exists (a referential-integrity violation).
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            sub = await repo.get(subscription_id)
            if sub is None:
                return None

            activity = await session.get(Activity, sub.activity_id)
            if activity is None:
                raise NotFoundError(
                    f"subscription {sub.id} references missing activity "
                    f"{sub.activity_id}"
                )
            location = await session.get(Location, sub.location_id)
            if location is None:
                raise NotFoundError(
                    f"subscription {sub.id} references missing location "
                    f"{sub.location_id}"
                )

            return SubscriptionForecastTarget(
                subscription=sub,
                activity_key=activity.key,
                center=GeoPoint(lat=location.lat, lon=location.lon),
                location_label=_location_label(location),
                search_radius_km=sub.search_radius_km,
            )

    async def summarize_for_user(self, user_id: int) -> list[SubscriptionSummary]:
        """Return a presentation-ready summary per subscription (Req 3.5).

        Produces one :class:`SubscriptionSummary` for **each** subscription the
        user owns, in stable id order, resolving the bound activity's display
        name and the location's label/place within a single
        ``session_scope`` so the caller (the ``/subscriptions`` formatter, task
        7.1) never has to touch ORM rows or open its own session. The output is
        pure data — no Telegram formatting happens here (Req 3.5, supports
        Property 19).

        Args:
            user_id: The owning user's id.

        Returns:
            One summary per owned subscription, each carrying the activity
            (key and display name), the location label and place, the search
            radius in km, and the notification mode. Empty when the user owns no
            subscriptions.

        Raises:
            NotFoundError: If a subscription references an activity or location
                row that no longer exists (a referential-integrity violation).
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            subscriptions = await repo.list_for_user(user_id)

            summaries: list[SubscriptionSummary] = []
            for sub in subscriptions:
                # Per-row lookups within the one unit of work; SQLAlchemy's
                # identity map serves repeated activities/locations from cache.
                activity = await session.get(Activity, sub.activity_id)
                if activity is None:
                    raise NotFoundError(
                        f"subscription {sub.id} references missing activity "
                        f"{sub.activity_id}"
                    )
                location = await session.get(Location, sub.location_id)
                if location is None:
                    raise NotFoundError(
                        f"subscription {sub.id} references missing location "
                        f"{sub.location_id}"
                    )
                summaries.append(
                    SubscriptionSummary(
                        subscription_id=sub.id,
                        activity_key=activity.key,
                        activity_display_name=activity.display_name,
                        location_label=_location_label(location),
                        location_place=_location_place(location),
                        search_radius_km=sub.search_radius_km,
                        notification_mode=sub.notification_mode,
                    )
                )
            return summaries

    async def remove(self, subscription_id: int) -> None:
        """Delete the subscription with id ``subscription_id`` (Req 3.6).

        Raises:
            NotFoundError: If no subscription with that id exists, so the caller
                can tell the user the selection was invalid rather than silently
                confirming a no-op deletion.
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            existing = await repo.get(subscription_id)
            if existing is None:
                raise NotFoundError(
                    f"subscription {subscription_id} does not exist"
                )
            await repo.delete(subscription_id)
            self._log.info("removed subscription %s", subscription_id)

    async def edit_radius(self, subscription_id: int, radius_km: float) -> Subscription:
        """Update a subscription's search radius and persist it (Req 3.7).

        The new radius is validated against the accepted ``[1, 200]`` km range
        before any change is made (Req 3.9, 3.10).

        Args:
            subscription_id: The subscription to edit.
            radius_km: The new search radius in km.

        Returns:
            The updated :class:`Subscription`.

        Raises:
            DomainValidationError: If ``radius_km`` is outside the accepted
                range (Req 3.9, 3.10).
            NotFoundError: If no subscription with that id exists.
        """
        # Req 3.9, 3.10 — validate before mutating anything.
        _validate_radius(radius_km)

        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            sub = await repo.get(subscription_id)
            if sub is None:
                raise NotFoundError(
                    f"subscription {subscription_id} does not exist"
                )
            sub.search_radius_km = radius_km
            await repo.update(sub)  # flush within the unit of work (Req 3.7)
            self._log.info(
                "updated subscription %s search radius to %.1f km",
                subscription_id,
                radius_km,
            )
            return sub

    # ------------------------------------------------------------------ #
    # Notification-preference mutators (consumed by the /settings flow,
    # task 7.6). Each persists a single preference on the subscription
    # through the same ``session_scope`` unit-of-work and raises
    # :class:`NotFoundError` for an unknown id so the bot can surface an
    # invalid selection rather than silently confirming a no-op.
    # ------------------------------------------------------------------ #

    async def set_notification_mode(
        self, subscription_id: int, mode: str
    ) -> Subscription:
        """Persist a subscription's notification mode (Req 10.2).

        Args:
            subscription_id: The subscription to edit.
            mode: A notification-mode key; must be one of the recognised
                ``NOTIFICATION_MODE_*`` keys.

        Returns:
            The updated :class:`Subscription`.

        Raises:
            DomainValidationError: If ``mode`` is not a recognised mode key.
            NotFoundError: If no subscription with that id exists.
        """
        # Validate before mutating so an unknown mode never reaches the DB.
        if mode not in ALL_NOTIFICATION_MODES:
            raise DomainValidationError(f"unknown notification mode {mode!r}")

        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            sub = await repo.get(subscription_id)
            if sub is None:
                raise NotFoundError(f"subscription {subscription_id} does not exist")
            sub.notification_mode = mode
            await repo.update(sub)
            self._log.info(
                "set subscription %s notification mode to %s", subscription_id, mode
            )
            return sub

    async def set_quiet_hours(
        self,
        subscription_id: int,
        start: time | None,
        end: time | None,
    ) -> Subscription:
        """Persist (or clear) a subscription's quiet hours (Req 11.1).

        Pass both ``start`` and ``end`` to set the daily quiet window, or both
        ``None`` to clear it. Supplying only one bound is rejected, since a
        half-open window is meaningless.

        Args:
            subscription_id: The subscription to edit.
            start: Inclusive quiet-hours start time, or ``None`` to clear.
            end: Inclusive quiet-hours end time, or ``None`` to clear.

        Returns:
            The updated :class:`Subscription`.

        Raises:
            DomainValidationError: If exactly one of ``start``/``end`` is given.
            NotFoundError: If no subscription with that id exists.
        """
        # Both-or-neither: a quiet window needs a start and an end.
        if (start is None) != (end is None):
            raise DomainValidationError(
                "quiet hours require both a start and an end time, or neither"
            )

        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            sub = await repo.get(subscription_id)
            if sub is None:
                raise NotFoundError(f"subscription {subscription_id} does not exist")
            sub.quiet_hours_start = start
            sub.quiet_hours_end = end
            await repo.update(sub)
            self._log.info(
                "set subscription %s quiet hours to %s-%s",
                subscription_id,
                start,
                end,
            )
            return sub

    async def set_muted(self, subscription_id: int, muted: bool) -> Subscription:
        """Persist a subscription's mute state (Req 11.3).

        Args:
            subscription_id: The subscription to edit.
            muted: ``True`` to mute the subscription, ``False`` to unmute it.

        Returns:
            The updated :class:`Subscription`.

        Raises:
            NotFoundError: If no subscription with that id exists.
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            sub = await repo.get(subscription_id)
            if sub is None:
                raise NotFoundError(f"subscription {subscription_id} does not exist")
            sub.muted = muted
            await repo.update(sub)
            self._log.info(
                "set subscription %s muted=%s", subscription_id, muted
            )
            return sub

    async def snooze(
        self, subscription_id: int, until: datetime | None
    ) -> Subscription:
        """Persist (or clear) a subscription's snooze deadline (Req 11.4).

        Args:
            subscription_id: The subscription to edit.
            until: The instant the snooze elapses, or ``None`` to clear it.

        Returns:
            The updated :class:`Subscription`.

        Raises:
            NotFoundError: If no subscription with that id exists.
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemySubscriptionRepository(session)
            sub = await repo.get(subscription_id)
            if sub is None:
                raise NotFoundError(f"subscription {subscription_id} does not exist")
            sub.snooze_until = until
            await repo.update(sub)
            self._log.info(
                "set subscription %s snooze_until to %s", subscription_id, until
            )
            return sub
