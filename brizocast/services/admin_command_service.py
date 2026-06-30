"""Admin command queue — the panel-side enqueue of admin actions.

The admin panel and the bot run as separate processes with no IPC channel; they
rendezvous on the shared SQLite database. When an admin triggers an action that
must execute inside the bot process (running a forecast check now, or
broadcasting a message to every user), the panel does not perform the work
itself: it inserts a row into the ``admin_commands`` table with status
``pending``. The bot later drains those rows oldest-first (task 2.2) and runs
the corresponding handler.

This module implements both the **enqueue** side (Req 8.1, 9.1) used by the
panel and the **drain** side (Req 8.3, 9.3, 12.3, 12.4) run by the bot, which
processes pending commands oldest-first with idempotency and per-command
isolation.

Each call follows the same unit-of-work boundary as the rest of the service
layer, opening sessions via
:func:`brizocast.database.session.session_scope`. To return the database-
generated primary key, the new row is flushed and its ``id`` read while still
inside the session scope (mirroring
:meth:`brizocast.config.overrides.ConfigOverrideStore.set`).

Requirements covered: 8.1, 8.3, 9.1, 9.3, 12.3, 12.4.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from brizocast.core.logging import get_logger
from brizocast.database.session import session_scope
from brizocast.models import AdminCommand, AdminCommandStatus
from brizocast.models.base import utcnow

if TYPE_CHECKING:
    from brizocast.core.container import SessionFactory

__all__ = [
    "AdminCommandType",
    "AdminCommandService",
    "CommandHandler",
    "DrainResult",
]

logger = get_logger(__name__)

# An async callback that performs the side effect for a claimed command. It
# receives the claimed :class:`AdminCommand` row (so handlers can read its
# ``payload``) and either completes normally — marking the command processed —
# or raises, marking it failed (per-command isolation, Req 12.4).
CommandHandler = Callable[[AdminCommand], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DrainResult:
    """Outcome counts from a single :meth:`AdminCommandService.drain` pass.

    Attributes:
        processed: Commands whose handler ran and completed successfully.
        failed: Commands marked ``failed`` — either their handler raised or no
            handler was registered for the command's type.
        skipped: Pending commands another concurrent drain claimed first (the
            guarded claim UPDATE matched no row), so this pass left them alone.
    """

    processed: int = 0
    failed: int = 0
    skipped: int = 0


class AdminCommandType(StrEnum):
    """The kinds of admin action the panel may enqueue for the bot to run."""

    RUN_FORECAST_CHECK = "run_forecast_check"
    BROADCAST = "broadcast"


class AdminCommandService:
    """Enqueues admin commands into the shared ``admin_commands`` table.

    Each method opens a single session from the injected ``async_sessionmaker``
    via :func:`session_scope`, which commits on success and rolls back on error.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        """Initialise the service.

        Args:
            session_factory: The application's ``async_sessionmaker``. Each call
                opens one session from it via ``session_scope``.
        """
        self._session_factory = session_factory

    async def enqueue(
        self, type_: AdminCommandType, payload: dict[str, Any] | None = None
    ) -> int:
        """Insert a new ``pending`` admin command and return its row id.

        Args:
            type_: The command kind; its ``value`` is stored in the row's
                ``type`` column.
            payload: Optional JSON-serialisable command arguments. Defaults to
                an empty dict when omitted.

        Returns:
            The database-generated primary key of the newly inserted row.
        """
        async with session_scope(self._session_factory) as session:
            row = AdminCommand(
                type=type_.value,
                payload=payload if payload is not None else {},
                status=AdminCommandStatus.PENDING,
            )
            session.add(row)
            await session.flush()  # populate the autoincrement id
            return row.id

    async def drain(
        self, handlers: Mapping[AdminCommandType, CommandHandler]
    ) -> DrainResult:
        """Process pending commands oldest-first, idempotently and in isolation.

        Pending commands are selected oldest-first (``created_at`` then ``id``)
        and processed one at a time. Each command is handled across its own set
        of short, independently-committed units of work so that a crash
        mid-drain leaves every already-completed command durably in a terminal
        state:

        1. **Claim** — a guarded ``UPDATE ... SET status='processing' WHERE
           id=:id AND status='pending'`` is committed in its own session. If it
           matches no row (``rowcount != 1``) another concurrent/re-entrant
           drain already claimed it, so this pass skips it. This guard is what
           prevents a double-claim and, together with selecting only ``PENDING``
           rows, gives idempotency: a ``processed``/``failed`` command is never
           reselected or re-run (Req 8.3, 9.3, 12.3).
        2. **Dispatch** — the handler registered for ``AdminCommandType(row.type)``
           is invoked with the claimed row. An unknown/unregistered type is
           marked ``failed`` (with a log) rather than left to loop forever.
        3. **Finalise** — on success the command is set ``processed``; on *any*
           handler exception the error is logged and the command set ``failed``.
           Either terminal state stamps ``processed_at``. A failure never aborts
           the pass — remaining commands are still processed (per-command
           isolation, Req 12.4).

        Args:
            handlers: Maps each command type to the async handler that performs
                its side effect. A type absent from this mapping is treated as
                unknown and its commands are marked ``failed``.

        Returns:
            A :class:`DrainResult` with the number of commands processed,
            failed, and skipped during this pass.
        """
        processed = 0
        failed = 0
        skipped = 0

        for command_id in await self._pending_ids():
            row = await self._claim(command_id)
            if row is None:
                # Another drain claimed it between selection and our guarded
                # UPDATE — leave it for that drain to finish.
                skipped += 1
                continue

            handler = self._resolve_handler(row, handlers)
            if handler is None:
                await self._finalise(command_id, AdminCommandStatus.FAILED)
                failed += 1
                continue

            try:
                await handler(row)
            except Exception:  # noqa: BLE001 - isolate per-command failures.
                logger.error(
                    "admin command %d (type=%s) failed; marking failed",
                    command_id,
                    row.type,
                    exc_info=True,
                )
                await self._finalise(command_id, AdminCommandStatus.FAILED)
                failed += 1
            else:
                await self._finalise(command_id, AdminCommandStatus.PROCESSED)
                processed += 1

        return DrainResult(processed=processed, failed=failed, skipped=skipped)

    async def _pending_ids(self) -> list[int]:
        """Return ids of pending commands, oldest-first (``created_at``, ``id``).

        Only ids are read here; each command is re-fetched under its own guarded
        claim so a status change between selection and claim cannot cause a
        double-claim.
        """
        async with session_scope(self._session_factory) as session:
            result = await session.execute(
                select(AdminCommand.id)
                .where(AdminCommand.status == AdminCommandStatus.PENDING)
                .order_by(AdminCommand.created_at, AdminCommand.id)
            )
            return list(result.scalars().all())

    async def _claim(self, command_id: int) -> AdminCommand | None:
        """Atomically claim a pending command, returning the claimed row.

        Runs the guarded ``pending -> processing`` UPDATE and, only if it
        matched exactly one row, loads and returns that row. Returns ``None``
        when the command was already claimed by another drain (so the caller
        skips it). The claim is committed before the handler runs.
        """
        async with session_scope(self._session_factory) as session:
            result = await session.execute(
                update(AdminCommand)
                .where(
                    AdminCommand.id == command_id,
                    AdminCommand.status == AdminCommandStatus.PENDING,
                )
                .values(status=AdminCommandStatus.PROCESSING)
            )
            if cast("CursorResult[Any]", result).rowcount != 1:
                return None
            claimed = await session.get(AdminCommand, command_id)
            return claimed

    @staticmethod
    def _resolve_handler(
        row: AdminCommand, handlers: Mapping[AdminCommandType, CommandHandler]
    ) -> CommandHandler | None:
        """Resolve the handler for ``row``'s type, or ``None`` if unhandled.

        An unrecognised ``type`` string (not a valid :class:`AdminCommandType`)
        or a type with no registered handler both yield ``None``, so the command
        is finalised as ``failed`` instead of looping forever.
        """
        try:
            command_type = AdminCommandType(row.type)
        except ValueError:
            logger.error(
                "admin command %d has unknown type %r; marking failed",
                row.id,
                row.type,
            )
            return None
        handler = handlers.get(command_type)
        if handler is None:
            logger.error(
                "admin command %d type %s has no registered handler; marking failed",
                row.id,
                command_type.value,
            )
        return handler

    async def _finalise(
        self, command_id: int, status: AdminCommandStatus
    ) -> None:
        """Stamp a command's terminal ``status`` and ``processed_at``.

        Committed in its own unit of work so the command's outcome is durable
        independently of the rest of the drain pass.
        """
        async with session_scope(self._session_factory) as session:
            await session.execute(
                update(AdminCommand)
                .where(AdminCommand.id == command_id)
                .values(status=status, processed_at=utcnow())
            )
