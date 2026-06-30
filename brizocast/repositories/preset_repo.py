"""SQLAlchemy implementation of :class:`PresetRepository` (Req 4.*, 19.*, 16.3).

All preset provenances (static defaults, user customs, AI-generated) live in
the one ``presets`` table with an identical parameter shape, so this single
repository serves them interchangeably (Req 16.10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from brizocast.models.preset import Preset
from brizocast.repositories.base import SqlAlchemyRepository

__all__ = ["SqlAlchemyPresetRepository"]


class SqlAlchemyPresetRepository(SqlAlchemyRepository[Preset]):
    """Persists default/region and user-custom presets.

    Conforms structurally to
    :class:`brizocast.core.ports.repositories.PresetRepository`.
    """

    model = Preset

    async def add(self, preset: Preset) -> Preset:
        """Persist a new preset and return the stored entity."""
        return await self._add(preset)

    async def get(self, preset_id: int) -> Preset | None:
        """Return the preset with id ``preset_id``, or ``None``."""
        return await self._get_by_pk(preset_id)

    async def list_defaults(self, region: str | None = None) -> list[Preset]:
        """Return default presets, optionally filtered to ``region``."""
        stmt = select(Preset).where(Preset.is_default.is_(True))
        if region is not None:
            stmt = stmt.where(Preset.region == region)
        result = await self._session.execute(stmt.order_by(Preset.id))
        return list(result.scalars().all())

    async def list_for_user(self, user_id: int) -> list[Preset]:
        """Return the custom presets owned by ``user_id``."""
        result = await self._session.execute(
            select(Preset)
            .where(Preset.owner_user_id == user_id)
            .order_by(Preset.id)
        )
        return list(result.scalars().all())

    async def update(self, preset: Preset) -> None:
        """Persist changes to an existing (session-attached) preset."""
        await self._flush()


if TYPE_CHECKING:
    from brizocast.core.ports.repositories import PresetRepository

    def _assert_conforms(repo: SqlAlchemyPresetRepository) -> PresetRepository:
        """Static-only check that the implementation satisfies the port."""
        return repo
