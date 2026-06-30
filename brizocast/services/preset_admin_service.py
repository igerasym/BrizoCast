"""Regional default-preset admin service (Req 5.2, 5.3, 5.4).

``PresetAdminService`` is the panel-side use case for managing **persisted
regional default presets** — rows in the existing ``presets`` table with
``owner_user_id IS NULL``, ``is_default=True`` and ``region=<name>``. These
DB-backed defaults are interchangeable with the bundled static defaults
everywhere in the bot (Req 16.10): ``PresetService.list_presets`` merges them
and ``resolve_effective_conditions`` reads them by id, so editing them tunes the
defaults offered per region without any code change (Req 5).

Design decision (see design "Regional presets: DB-backed rows"): rather than
editing the static-in-code ``_STATIC_PRESETS``, the panel upserts persisted
default rows. The static defaults remain the immutable seed/fallback.

Unit of work
------------
Like the other services, this one is injected with an ``async_sessionmaker`` and
opens its own :func:`~brizocast.database.session.session_scope` per method,
driving :class:`~brizocast.repositories.preset_repo.SqlAlchemyPresetRepository`
within it. Writes (add / update) commit when the scope exits normally.

Validation
----------
A regional preset's minimum wave height must be ``<=`` its maximum wave height
(Req 5.4), mirroring the bot's existing ``SurfConditions`` rule. The check runs
on **both** create and edit and rejects the operation *without persisting* by
raising :class:`PresetValidationError` before any session write.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.core.errors import NotFoundError
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.database.session import session_scope
from brizocast.models.preset import Preset
from brizocast.repositories.preset_repo import SqlAlchemyPresetRepository

__all__ = ["PresetAdminService", "PresetValidationError"]


class PresetValidationError(ValueError):
    """Raised when a regional preset violates a domain rule (Req 5.4).

    The sole rule enforced here is ``min_wave_m <= max_wave_m`` (an inverted
    wave band is rejected). It subclasses :class:`ValueError` so callers that
    catch either the domain type or a plain value error both surface the
    message; the panel router renders it as a 400 with the specific message and
    leaves the ``presets`` table unchanged.
    """


class PresetAdminService:
    """Create and edit persisted regional default presets (Req 5.2, 5.3, 5.4)."""

    #: Fields an ``edit_regional_preset`` call may mutate. Provenance/identity
    #: columns (``id``, ``owner_user_id``, ``is_default``, ``ai_generated``) are
    #: intentionally excluded so a regional default stays a regional default.
    _EDITABLE_FIELDS: Final[frozenset[str]] = frozenset(
        {
            "name",
            "region",
            "min_wave_m",
            "max_wave_m",
            "min_period_s",
            "max_wind_kmh",
            "preferred_wind_dir",
            "preferred_swell_dir",
            "min_alert_score",
        }
    )

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            session_factory: Async session maker providing the unit-of-work
                boundary; each method runs inside its own ``session_scope``.
            logger: Optional bound logger; one is created when omitted.
        """
        self._session_factory = session_factory
        self._log = logger or get_logger(__name__)

    # -- listing -------------------------------------------------------- #

    async def list_regional_presets(self) -> list[Preset]:
        """Return all persisted default presets (Req 5.1).

        Delegates to ``repo.list_defaults()`` (rows with ``is_default=True``),
        which are the editable regional defaults this service manages.
        """
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyPresetRepository(session)
            return await repo.list_defaults()

    # -- create --------------------------------------------------------- #

    async def create_regional_preset(
        self,
        region: str,
        name: str,
        min_wave_m: float,
        max_wave_m: float,
        min_period_s: float,
        max_wind_kmh: float,
        preferred_wind_dir: str | None = None,
        preferred_swell_dir: str | None = None,
        min_alert_score: int | None = None,
    ) -> Preset:
        """Persist a new regional default preset."""
        self._validate_wave_range(min_wave_m, max_wave_m)

        preset = Preset(
            owner_user_id=None,
            name=name,
            region=region,
            is_default=True,
            ai_generated=False,
            min_wave_m=min_wave_m,
            max_wave_m=max_wave_m,
            min_period_s=min_period_s,
            max_wind_kmh=max_wind_kmh,
            preferred_wind_dir=preferred_wind_dir,
            preferred_swell_dir=preferred_swell_dir,
            min_alert_score=min_alert_score,
        )
        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyPresetRepository(session)
            created = await repo.add(preset)
            self._log.info(
                "created regional default preset %r for region=%r",
                created.name,
                region,
            )
            return created

    # -- edit ----------------------------------------------------------- #

    async def edit_regional_preset(self, preset_id: int, **fields: object) -> Preset:
        """Edit an existing regional default preset (Req 5.2, 5.4).

        Loads the preset via ``repo.get``, mutates the allowed fields, then
        persists via ``repo.update``. The resulting wave band (combining the
        submitted fields with the unchanged existing values) is validated so an
        edit cannot leave an inverted band (Req 5.4).

        Args:
            preset_id: The id of the persisted default preset to edit.
            **fields: A subset of :attr:`_EDITABLE_FIELDS` to overwrite.

        Returns:
            The updated :class:`Preset`.

        Raises:
            PresetValidationError: If an unknown field is supplied, a wave value
                is non-numeric, or the resulting ``min_wave_m`` exceeds
                ``max_wave_m``; the row is **not** persisted (Req 5.4).
            NotFoundError: If no preset with ``preset_id`` exists.
        """
        unknown = set(fields) - self._EDITABLE_FIELDS
        if unknown:
            raise PresetValidationError(
                f"cannot edit unknown preset field(s): {', '.join(sorted(unknown))}"
            )

        async with session_scope(self._session_factory) as session:
            repo = SqlAlchemyPresetRepository(session)
            preset = await repo.get(preset_id)
            if preset is None:
                raise NotFoundError(f"preset {preset_id} does not exist")

            # Resolve the effective wave band: submitted value if present, else
            # the current stored value. Validate before mutating (Req 5.4).
            new_min = (
                _as_float(fields["min_wave_m"], "min_wave_m")
                if "min_wave_m" in fields
                else preset.min_wave_m
            )
            new_max = (
                _as_float(fields["max_wave_m"], "max_wave_m")
                if "max_wave_m" in fields
                else preset.max_wave_m
            )
            self._validate_wave_range(new_min, new_max)

            for key, value in fields.items():
                setattr(preset, key, value)
            await repo.update(preset)
            self._log.info("edited regional default preset %s", preset_id)
            return preset

    # -- validation ----------------------------------------------------- #

    @staticmethod
    def _validate_wave_range(min_wave_m: float, max_wave_m: float) -> None:
        """Reject an inverted wave band (Req 5.4).

        Raises:
            PresetValidationError: If ``min_wave_m`` exceeds ``max_wave_m``.
        """
        if min_wave_m > max_wave_m:
            raise PresetValidationError(
                "minimum wave height must be less than or equal to the maximum "
                f"(got min={min_wave_m:g} m, max={max_wave_m:g} m)"
            )


def _as_float(value: object, field: str) -> float:
    """Coerce a submitted ``**fields`` value to ``float`` for validation.

    Raises:
        PresetValidationError: If ``value`` is not a real number (``bool`` is
            rejected too, since a boolean wave height is meaningless).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PresetValidationError(
            f"{field} must be a number (got {value!r})"
        )
    return float(value)
