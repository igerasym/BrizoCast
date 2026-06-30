"""Preset and custom-conditions service (Req 4.*, 16.10).

``PresetService`` is the application-layer use case for everything to do with a
subscription's scoring conditions:

* **Listing** the available defaults plus the user's own presets (Req 4.3) via
  :meth:`list_presets`.
* **Selecting** a default (or custom) preset for a subscription (Req 4.4) via
  :meth:`select_default`.
* **Persisting** per-subscription custom conditions, rejecting an inverted wave
  band (Req 4.5, 4.6, 4.8) via :meth:`create_custom_conditions`.
* **Resolving** the *effective* conditions the scorer consumes, in the order
  custom → selected preset → the region's first default (Req 4.7, 4.9;
  Property 14) via :meth:`resolve_effective_conditions`.

Provenance-agnostic by design
------------------------------
All persisted presets — region defaults, user customs, and AI-generated presets
— share one parameter shape and live in the single ``presets`` table (Req
16.10). This service therefore never branches on *how* a preset was produced; an
AI-generated preset (task 9.2) is handled exactly like a static or user preset.

The preset → conditions mapping
-------------------------------
A :class:`~brizocast.models.preset.Preset` (and the bundled
:class:`~brizocast.activities.surf.presets.DefaultPreset`) carries the surf
parameter shape — min/max wave, min period, max wind, and preferred wind/swell
directions — which maps **field-for-field** onto a
:class:`~brizocast.activities.surf.conditions.SurfConditions`. Two
representation bridges apply:

* **Directions.** Presets and custom conditions store directions as 16-point
  compass strings (``String(16)`` columns); :class:`SurfConditions` and the
  scorer use degrees. :func:`~brizocast.activities.surf.directions.compass_to_degrees`
  / :func:`~brizocast.activities.surf.directions.degrees_to_compass` bridge them
  (a degrees → compass round-trip snaps to the nearest 22.5° point).
* **Custom-only fields.** ``daylight_only`` and ``tide_preference`` exist on
  custom conditions but not on a preset; when conditions come from a preset they
  default to off / none.

Unit of work
------------
Like the other services, ``PresetService`` is injected with an
``async_sessionmaker`` and opens its own :func:`~brizocast.database.session.session_scope`
per method, driving the preset / custom-condition / subscription repositories
within it.

Requirements covered: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.9, 15.6, 15.8, 16.10,
19.5, 19.6, 19.7, 19.8, 19.9, 19.10 (supports Properties 14, 18, and 23).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.activities.surf.conditions import SurfConditions, TidePreference
from brizocast.activities.surf.directions import (
    compass_to_degrees,
    degrees_to_compass,
)
from brizocast.activities.surf.presets import (
    DefaultPreset,
    first_default_for_region,
    static_presets_for_region,
)
from brizocast.core.domain.conditions import PresetParams
from brizocast.core.errors import (
    DomainValidationError,
    NotFoundError,
    ProviderRequestError,
)
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.core.ports.ai_provider import AIProvider
from brizocast.database.session import session_scope
from brizocast.models.custom_condition import CustomCondition
from brizocast.models.preset import Preset
from brizocast.models.subscription import Subscription
from brizocast.repositories.condition_repo import SqlAlchemyCustomConditionRepository
from brizocast.repositories.preset_repo import SqlAlchemyPresetRepository
from brizocast.repositories.subscription_repo import SqlAlchemySubscriptionRepository

__all__ = [
    "PresetOption",
    "PresetSource",
    "PresetService",
    "surf_conditions_from_fields",
]


class PresetSource(str, Enum):
    """Where a listed preset came from, for the ``/presets`` view (Req 4.3)."""

    #: A bundled static Default_Preset shipped in code (no database row).
    STATIC_DEFAULT = "static_default"
    #: A default preset persisted in the ``presets`` table (e.g. AI-generated).
    PERSISTED_DEFAULT = "persisted_default"
    #: A custom preset owned by the requesting user.
    USER_CUSTOM = "user_custom"
    #: An AI-generated region default produced on the fly by the AIProvider.
    #: Ephemeral — it carries no database id until materialised (see
    #: :meth:`PresetService.get_region_presets`).
    AI_GENERATED = "ai_generated"


@dataclass(frozen=True)
class PresetOption:
    """A single selectable preset surfaced by :meth:`PresetService.list_presets`.

    Unifies bundled static defaults (which have no database id) with persisted
    presets so the ``/presets`` handler can render and select either kind.
    ``preset_id`` is the database id used by :meth:`PresetService.select_default`;
    it is ``None`` for a static default (which must be materialised before it can
    be attached to a subscription).
    """

    name: str
    region: str | None
    params: PresetParams
    source: PresetSource
    preset_id: int | None
    ai_generated: bool

    @property
    def is_default(self) -> bool:
        """Whether this option is a default (static or persisted), not a custom."""
        return self.source is not PresetSource.USER_CUSTOM


def surf_conditions_from_fields(
    *,
    min_wave_m: float,
    max_wave_m: float,
    min_period_s: float,
    max_wind_kmh: float,
    preferred_wind_dir_deg: float | None = None,
    preferred_swell_dir_deg: float | None = None,
    tide_preference: TidePreference | None = None,
    daylight_only: bool = False,
) -> SurfConditions:
    """Build :class:`SurfConditions` from raw fields, surfacing errors cleanly.

    A convenience for the custom-conditions conversation (task 7.5), which
    collects primitive fields. Any validation failure — most notably a minimum
    wave height greater than the maximum (Req 4.8) or an out-of-range
    direction — is re-raised as a :class:`~brizocast.core.errors.DomainValidationError`
    so the bot can surface the message and re-request the value rather than
    leaking a Pydantic ``ValidationError``.
    """
    try:
        return SurfConditions(
            min_wave_m=min_wave_m,
            max_wave_m=max_wave_m,
            min_period_s=min_period_s,
            max_wind_kmh=max_wind_kmh,
            preferred_wind_dir_deg=preferred_wind_dir_deg,
            preferred_swell_dir_deg=preferred_swell_dir_deg,
            tide_preference=tide_preference,
            daylight_only=daylight_only,
        )
    except ValidationError as exc:
        raise DomainValidationError(
            f"invalid custom conditions: {exc.errors()[0]['msg'] if exc.errors() else exc}"
        ) from exc


class PresetService:
    """List presets, select defaults, and resolve effective conditions (Req 4.*)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ai_provider: AIProvider | None = None,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            session_factory: Async session maker providing the unit-of-work
                boundary; each method runs inside its own ``session_scope``.
            ai_provider: Optional :class:`AIProvider` used by
                :meth:`get_region_presets` to generate region defaults. When
                omitted (or ``None``) AI generation is treated as unavailable and
                only bundled static defaults are returned (Req 15.8, 19.6, 19.7).
                The container injects the resolved provider (task 11.1); a
                :class:`NullAIProvider` (``is_available()`` ``False``) is
                equivalent to ``None`` here.
            logger: Optional bound logger; one is created when omitted.
        """
        self._session_factory = session_factory
        self._ai_provider = ai_provider
        self._log = logger or get_logger(__name__)

    # -- listing -------------------------------------------------------- #

    async def list_presets(
        self, user_id: int, region: str | None = None
    ) -> list[PresetOption]:
        """Return the available defaults plus the user's custom presets (Req 4.3).

        The result combines, in order:

        1. bundled static defaults for ``region`` (all defaults when ``region``
           is ``None``; the generic fallback for an unknown region) (Req 4.1);
        2. any persisted default presets for ``region`` (e.g. AI-generated ones,
           which share the same table and shape, Req 16.10); and
        3. the requesting user's own custom presets.

        Args:
            user_id: The user whose custom presets are included.
            region: Optional region filter for the defaults.

        Returns:
            A list of :class:`PresetOption`s suitable for the ``/presets`` view.
        """
        options: list[PresetOption] = [
            self._option_from_default(default)
            for default in static_presets_for_region(region)
        ]

        async with session_scope(self._session_factory) as session:
            presets = SqlAlchemyPresetRepository(session)
            for persisted_default in await presets.list_defaults(region):
                options.append(
                    self._option_from_preset(
                        persisted_default, PresetSource.PERSISTED_DEFAULT
                    )
                )
            for custom in await presets.list_for_user(user_id):
                options.append(
                    self._option_from_preset(custom, PresetSource.USER_CUSTOM)
                )
        return options

    async def get_region_presets(
        self, region: str | None, activity_key: str = "surf"
    ) -> list[PresetOption]:
        """Return the region's default presets, AI-augmented when available.

        Resolution (design "PresetService.get_region_presets" pseudocode):

        1. Start from the bundled **static** defaults for ``region``, which are
           always available so a usable default always exists (Req 19.6).
        2. When the injected :class:`AIProvider` reports
           :meth:`~AIProvider.is_available` (AI enabled and keyed), attempt to
           generate a region default and **prepend** it to the static defaults.
           The AI preset shares the exact :class:`PresetParams` shape of a static
           preset, so the two are interchangeable everywhere (Req 19.5, 16.10).
        3. If the provider raises :class:`ProviderRequestError` — or any other
           failure — log it with provider/context and **silently fall back** to
           the static defaults, so a scheduler run continues uninterrupted
           (Req 19.8, 19.9).

        When no AI provider is injected, or the provider is unavailable
        (disabled / unkeyed ``NullAIProvider``), only the static defaults are
        returned (Req 15.6, 15.8, 19.6, 19.7).

        The AI-generated preset is **ephemeral**: it is generated on the fly and
        returned as a :class:`PresetOption` with ``preset_id=None``. It is not
        persisted here. Should a caller choose to materialise it (e.g. when a
        user selects it), it must be stored in the same ``presets`` table with
        the same shape as any other default (Req 16.10) before it can be
        attached to a subscription via :meth:`select_default`.

        Args:
            region: The region to resolve defaults for. When ``None`` the full
                bundled default catalogue is returned and AI generation is
                skipped (there is no single region to generate for).
            activity_key: The activity to generate for; defaults to ``"surf"``.

        Returns:
            The region's :class:`PresetOption`s, with any AI-generated default
            first, followed by the bundled static defaults.
        """
        # Static defaults are always available (Req 19.6).
        static_options: list[PresetOption] = [
            self._option_from_default(default)
            for default in static_presets_for_region(region)
        ]

        provider = self._ai_provider
        # AI disabled / unkeyed / not injected → static only (Req 15.8, 19.6/7).
        if region is None or provider is None or not provider.is_available():
            return static_options

        try:
            ai_params = await provider.generate_region_preset(region, activity_key)
        except ProviderRequestError as exc:
            # Logged with provider/context; fall back silently (Req 19.8, 19.9).
            self._log.warning(
                "AI preset generation failed; using static defaults "
                "(provider=%s, region=%r, activity=%r): %s",
                provider.key,
                region,
                activity_key,
                exc,
            )
            return static_options
        except Exception as exc:  # noqa: BLE001 - any provider failure degrades.
            # Defensive: a misbehaving provider must never abort a scheduler run.
            self._log.warning(
                "AI preset generation raised unexpectedly; using static defaults "
                "(provider=%s, region=%r, activity=%r): %s",
                provider.key,
                region,
                activity_key,
                exc,
            )
            return static_options

        ai_option = self._option_from_ai_params(region, ai_params)
        self._log.info(
            "prepended AI-generated preset for region=%r activity=%r (provider=%s)",
            region,
            activity_key,
            provider.key,
        )
        # Prepend the AI default (interchangeable shape, Req 19.5, 16.10).
        return [ai_option, *static_options]

    async def select_default(
        self, subscription_id: int, preset_id: int
    ) -> Subscription:
        """Associate persisted preset ``preset_id`` with a subscription (Req 4.4).

        Sets the subscription's ``preset_id`` so the scorer uses that preset when
        the subscription has no custom conditions (Req 4.7). The preset must
        already exist in the ``presets`` table (a bundled static default has no
        database row until materialised; see :class:`PresetOption`).

        Args:
            subscription_id: The subscription to update.
            preset_id: The id of the preset to attach.

        Returns:
            The updated :class:`Subscription`.

        Raises:
            NotFoundError: If the subscription or the preset does not exist.
        """
        async with session_scope(self._session_factory) as session:
            presets = SqlAlchemyPresetRepository(session)
            preset = await presets.get(preset_id)
            if preset is None:
                raise NotFoundError(f"preset {preset_id} does not exist")

            subscriptions = SqlAlchemySubscriptionRepository(session)
            subscription = await subscriptions.get(subscription_id)
            if subscription is None:
                raise NotFoundError(
                    f"subscription {subscription_id} does not exist"
                )

            subscription.preset_id = preset_id
            await subscriptions.update(subscription)
            self._log.info(
                "associated preset %s with subscription %s",
                preset_id,
                subscription_id,
            )
            return subscription

    # -- custom conditions ---------------------------------------------- #

    async def create_custom_conditions(
        self, subscription_id: int, conditions: SurfConditions
    ) -> CustomCondition:
        """Persist custom conditions for a subscription (Req 4.5, 4.6).

        Rejects an inverted wave band — minimum wave height greater than maximum
        — with a :class:`~brizocast.core.errors.DomainValidationError` (Req 4.8;
        supports Property 18). :class:`SurfConditions` already enforces this at
        construction, but the guard is repeated here so the rule is surfaced
        cleanly regardless of how the value object was built.

        If custom conditions already exist for the subscription they are
        updated in place (a subscription has at most one override, Req 4.7);
        otherwise a new row is inserted. Once present, these conditions override
        any selected preset during scoring.

        Args:
            subscription_id: The subscription the conditions belong to.
            conditions: The fully-validated surf conditions to persist.

        Returns:
            The persisted :class:`CustomCondition`.

        Raises:
            DomainValidationError: If ``min_wave_m`` exceeds ``max_wave_m``
                (Req 4.8).
        """
        # Req 4.8 — defensive re-check so the rule is surfaced cleanly.
        if conditions.min_wave_m > conditions.max_wave_m:
            raise DomainValidationError(
                "minimum wave height must be less than or equal to the maximum "
                f"(got min={conditions.min_wave_m:g} m, max={conditions.max_wave_m:g} m)"
            )

        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyCustomConditionRepository(session)
            existing = await repo.get_for_subscription(subscription_id)
            if existing is not None:
                self._apply_conditions(existing, conditions)
                await repo.update(existing)
                self._log.info(
                    "updated custom conditions for subscription %s", subscription_id
                )
                return existing

            created = await repo.add(
                self._build_custom_condition(subscription_id, conditions)
            )
            self._log.info(
                "created custom conditions for subscription %s", subscription_id
            )
            return created

    # -- effective resolution ------------------------------------------- #

    async def resolve_effective_conditions(
        self, subscription: Subscription, *, region: str | None = None
    ) -> SurfConditions:
        """Resolve the conditions the scorer should use (Req 4.7, 4.9; Property 14).

        Resolution order:

        1. the subscription's **custom conditions** when present (Req 4.7);
        2. else the subscription's **selected preset** (Req 4.4);
        3. else the **region's first default** preset for the subscription's
           location (Req 4.9).

        The region's first default always exists (an unknown or ``None`` region
        falls back to the generic all-round default), so this method always
        returns usable conditions.

        Args:
            subscription: The subscription to resolve conditions for. Only its
                loaded scalar attributes (``id``, ``preset_id``) are read, so a
                detached instance is safe.
            region: The region of the subscription's location, used to pick the
                last-resort default. The scheduler supplies the discovered
                spot's region; when omitted the generic default is used. (A
                ``Location`` carries no region, so the caller resolves it.)

        Returns:
            The effective :class:`SurfConditions` for scoring.
        """
        async with session_scope(self._session_factory) as session:
            conditions = SqlAlchemyCustomConditionRepository(session)
            custom = await conditions.get_for_subscription(subscription.id)
            if custom is not None:
                self._log.debug(
                    "subscription %s uses custom conditions", subscription.id
                )
                return self._custom_to_conditions(custom)

            if subscription.preset_id is not None:
                presets = SqlAlchemyPresetRepository(session)
                preset = await presets.get(subscription.preset_id)
                if preset is not None:
                    self._log.debug(
                        "subscription %s uses selected preset %s",
                        subscription.id,
                        subscription.preset_id,
                    )
                    return self._preset_to_conditions(preset)

        # Last resort: the region's first bundled default (Req 4.9).
        default = first_default_for_region(region)
        self._log.debug(
            "subscription %s falls back to region default %r (region=%r)",
            subscription.id,
            default.name,
            region,
        )
        return default.to_conditions()

    # -- mapping helpers ------------------------------------------------ #

    @staticmethod
    def _option_from_default(default: DefaultPreset) -> PresetOption:
        """Build a :class:`PresetOption` from a bundled static default."""
        return PresetOption(
            name=default.name,
            region=default.region,
            params=default.params,
            source=PresetSource.STATIC_DEFAULT,
            preset_id=None,
            ai_generated=False,
        )

    @staticmethod
    def _option_from_preset(preset: Preset, source: PresetSource) -> PresetOption:
        """Build a :class:`PresetOption` from a persisted preset row."""
        return PresetOption(
            name=preset.name,
            region=preset.region,
            params=PresetService._preset_to_params(preset),
            source=source,
            preset_id=preset.id,
            ai_generated=preset.ai_generated,
        )

    @staticmethod
    def _option_from_ai_params(region: str, params: PresetParams) -> PresetOption:
        """Build an ephemeral AI-generated :class:`PresetOption` (Req 19.5).

        The option carries ``preset_id=None`` (it is not persisted) and
        ``ai_generated=True`` to record its provenance; its ``params`` share the
        exact static-preset shape so it is interchangeable everywhere.
        """
        return PresetOption(
            name=f"{region} — AI Suggested",
            region=region,
            params=params,
            source=PresetSource.AI_GENERATED,
            preset_id=None,
            ai_generated=True,
        )

    @staticmethod
    def _preset_to_params(preset: Preset) -> PresetParams:
        """Map a persisted :class:`Preset` onto :class:`PresetParams` (degrees)."""
        return PresetParams(
            min_wave_m=preset.min_wave_m,
            max_wave_m=preset.max_wave_m,
            min_period_s=preset.min_period_s,
            max_wind_kmh=preset.max_wind_kmh,
            preferred_wind_dir_deg=compass_to_degrees(preset.preferred_wind_dir),
            preferred_swell_dir_deg=compass_to_degrees(preset.preferred_swell_dir),
        )

    @staticmethod
    def _preset_to_conditions(preset: Preset) -> SurfConditions:
        """Project a persisted :class:`Preset` onto :class:`SurfConditions`.

        Custom-only fields (``daylight_only``, ``tide_preference``) are off/none
        since a preset does not carry them.
        """
        return SurfConditions(
            min_wave_m=preset.min_wave_m,
            max_wave_m=preset.max_wave_m,
            min_period_s=preset.min_period_s,
            max_wind_kmh=preset.max_wind_kmh,
            preferred_wind_dir_deg=compass_to_degrees(preset.preferred_wind_dir),
            preferred_swell_dir_deg=compass_to_degrees(preset.preferred_swell_dir),
            tide_preference=None,
            daylight_only=False,
        )

    @staticmethod
    def _custom_to_conditions(custom: CustomCondition) -> SurfConditions:
        """Map persisted :class:`CustomCondition` onto :class:`SurfConditions`."""
        return SurfConditions(
            min_wave_m=custom.min_wave_m,
            max_wave_m=custom.max_wave_m,
            min_period_s=custom.min_period_s,
            max_wind_kmh=custom.max_wind_kmh,
            preferred_wind_dir_deg=compass_to_degrees(custom.acceptable_wind_dir),
            preferred_swell_dir_deg=compass_to_degrees(custom.acceptable_swell_dir),
            tide_preference=_parse_tide(custom.tide_preference),
            daylight_only=custom.daylight_only,
        )

    @staticmethod
    def _build_custom_condition(
        subscription_id: int, conditions: SurfConditions
    ) -> CustomCondition:
        """Build a new :class:`CustomCondition` row from :class:`SurfConditions`."""
        condition = CustomCondition(subscription_id=subscription_id)
        PresetService._apply_conditions(condition, conditions)
        return condition

    @staticmethod
    def _apply_conditions(
        condition: CustomCondition, conditions: SurfConditions
    ) -> None:
        """Copy :class:`SurfConditions` fields onto a (new or existing) row.

        Degree directions are quantised to 16-point compass strings to fit the
        column shape; the optional tide preference is stored as its value.
        """
        condition.min_wave_m = conditions.min_wave_m
        condition.max_wave_m = conditions.max_wave_m
        condition.min_period_s = conditions.min_period_s
        condition.max_wind_kmh = conditions.max_wind_kmh
        condition.acceptable_wind_dir = degrees_to_compass(
            conditions.preferred_wind_dir_deg
        )
        condition.acceptable_swell_dir = degrees_to_compass(
            conditions.preferred_swell_dir_deg
        )
        condition.tide_preference = (
            conditions.tide_preference.value
            if conditions.tide_preference is not None
            else None
        )
        condition.daylight_only = conditions.daylight_only


def _parse_tide(value: str | None) -> TidePreference | None:
    """Parse a stored tide-preference string into a :class:`TidePreference`.

    Unknown / unset values map to ``None`` so a malformed stored value degrades
    to "no preference" rather than raising during scoring resolution.
    """
    if value is None:
        return None
    try:
        return TidePreference(value)
    except ValueError:
        return None
