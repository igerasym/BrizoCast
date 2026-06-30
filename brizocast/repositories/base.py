"""Typed base for SQLAlchemy repository implementations.

Session strategy (the unit-of-work boundary)
---------------------------------------------
Every repository in this package is **thin and composable**: it is constructed
with an already-open :class:`~sqlalchemy.ext.asyncio.AsyncSession` and performs
its reads and writes against that session. The repository **does not own** the
session lifecycle — it never commits, rolls back, or closes. Instead, the
*caller* (a service or a scheduler job) opens a single session via
:func:`brizocast.database.session.session_scope`, shares it with one or more
repositories, and that context manager provides the transactional unit of work
(commit on success, rollback on error, always close)::

    async with session_scope(session_factory) as session:
        users = SqlAlchemyUserRepository(session)
        plans = SqlAlchemyPlanRepository(session)
        user = await users.add(User(...))
        await plans.add(Plan(user_id=user.id, ...))
    # <- the single transaction commits here, atomically across both repos

Rationale for injecting a session rather than a ``async_sessionmaker``:

* **Composability / atomicity.** Multiple repositories sharing one session
  participate in one transaction, so a multi-entity use case (e.g. create a
  user *and* its Free plan) is all-or-nothing without the repositories knowing
  about one another.
* **Thinness.** Repositories contain only query/mapping logic; transaction
  policy lives in exactly one place (``session_scope``), matching the design's
  "repositories isolate persistence from the service layer" goal (Req 16.3).
* **Testability.** A test can open one session, exercise a repository, and
  assert within the same unit of work.

Write semantics
----------------
* :meth:`SqlAlchemyRepository._add` adds the entity and ``flush``es so the
  database assigns the primary key (and any defaults) while remaining inside
  the caller's transaction; the populated entity is returned.
* ``update`` methods ``flush`` pending changes for an entity **already attached
  to this session** (the normal unit-of-work case: the entity was loaded or
  added through the same session). They do not commit.
* ``delete`` methods load the entity (so ORM-level cascades fire) and issue a
  session delete; the actual removal is committed by the surrounding
  ``session_scope``.

This module targets ``mypy --strict`` and uses the SQLAlchemy 2.x async API.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from brizocast.core.logging import BoundLogger, get_logger
from brizocast.models.base import Base

__all__ = ["SqlAlchemyRepository"]

# Bound to the shared declarative base so every concrete repository is generic
# over a real ORM model type and the common helpers stay type-safe.
ModelT = TypeVar("ModelT", bound=Base)


class SqlAlchemyRepository(Generic[ModelT]):
    """Common base for SQLAlchemy repositories over a single ORM model.

    Holds the injected :class:`AsyncSession` (the caller-owned unit of work,
    see the module docstring) and a bound logger, and exposes small protected
    helpers shared by the concrete repositories. Subclasses set
    :attr:`model` to their ORM model class and implement their port's methods.
    """

    #: The ORM model class this repository manages. Set by each subclass.
    model: type[ModelT]

    def __init__(
        self,
        session: AsyncSession,
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        """Bind the repository to an open session.

        Args:
            session: An open async session whose transaction lifecycle is owned
                by the caller (typically via
                :func:`brizocast.database.session.session_scope`).
            logger: Optional bound logger; one is created when omitted.
        """
        self._session = session
        self._log = logger or get_logger(type(self).__module__)

    # -- shared write/read helpers -------------------------------------- #

    async def _add(self, entity: ModelT) -> ModelT:
        """Add ``entity`` to the session and flush to assign its primary key.

        The flush stays within the caller's transaction; nothing is committed
        here. The same (now persistent) instance is returned with server- and
        default-populated columns available.
        """
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def _get_by_pk(self, entity_id: int) -> ModelT | None:
        """Return the managed-model row with primary key ``entity_id`` or ``None``."""
        return await self._session.get(self.model, entity_id)

    async def _flush(self) -> None:
        """Flush pending changes for entities attached to this session.

        Used by ``update`` methods: an entity loaded or added through this
        session is already tracked, so flushing emits the UPDATE while keeping
        the caller's transaction open.
        """
        await self._session.flush()

    async def _delete_instance(self, entity: ModelT) -> None:
        """Delete an attached ``entity`` (ORM cascades fire) and flush."""
        await self._session.delete(entity)
        await self._session.flush()
