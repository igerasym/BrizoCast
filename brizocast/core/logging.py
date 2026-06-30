"""Structured, resilient logging setup for BrizoCast.

This module is a small, dependency-free wrapper around Python's standard
:mod:`logging`. It provides:

* :func:`configure_logging` — install a single structured handler on the
  package logger with an explicit severity level (Req 18.6).
* :func:`get_logger` — obtain a :class:`BoundLogger` that can carry structured
  contextual fields such as ``provider``, ``subscription_id`` and ``spot_key``
  (Req 18.2 / 18.6) which are rendered alongside every message.
* A resilient handler that guarantees a failure to write a log entry never
  propagates, so the process keeps running forecast-check jobs (Req 18.5).

It intentionally has **no** dependency on the rest of the application and does
not wire itself into the bot or scheduler — composition happens later at the
application root.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from types import TracebackType
from typing import Final

__all__ = [
    "BoundLogger",
    "PACKAGE_LOGGER_NAME",
    "configure_logging",
    "get_logger",
]

# All application loggers live under this namespace so a single handler,
# installed by ``configure_logging``, captures every package log record.
PACKAGE_LOGGER_NAME: Final[str] = "brizocast"

# Key under which structured context is stashed on a ``LogRecord`` via the
# standard ``extra=`` mechanism. Using a single nested key avoids clobbering
# reserved ``LogRecord`` attributes.
_CONTEXT_KEY: Final[str] = "context"

_DEFAULT_FORMAT: Final[str] = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DEFAULT_DATEFMT: Final[str] = "%Y-%m-%dT%H:%M:%S%z"

# ``exc_info`` accepted by the standard logging calls.
_ExcInfo = (
    bool
    | BaseException
    | tuple[type[BaseException], BaseException, TracebackType | None]
    | tuple[None, None, None]
    | None
)


class _StructuredFormatter(logging.Formatter):
    """Formatter that appends structured context fields to each line.

    Any context bound to the record (via :class:`BoundLogger`) is rendered as a
    trailing ``[key=value ...]`` segment, keeping records human-readable while
    still carrying the structured fields operators care about.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        context = getattr(record, _CONTEXT_KEY, None)
        if isinstance(context, Mapping) and context:
            rendered = " ".join(f"{key}={value}" for key, value in context.items())
            return f"{base} [{rendered}]"
        return base


class _ResilientStreamHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Stream handler that never lets a sink write failure escape.

    If emitting a record raises (e.g. the underlying stream is closed or a
    formatting error occurs), the failure is swallowed so the application keeps
    running its forecast-check jobs (Req 18.5).
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except Exception:  # noqa: BLE001 - logging must never crash the caller.
            try:
                self.handleError(record)
            except Exception:  # noqa: BLE001 - defensive: handleError must not raise.
                pass


class BoundLogger:
    """A logger carrying immutable structured context.

    Wraps a standard :class:`logging.Logger` and injects bound context (such as
    ``provider``, ``subscription_id`` or ``spot_key``) into every emitted
    record. Instances are immutable: :meth:`bind` returns a new
    ``BoundLogger`` with the merged context.
    """

    __slots__ = ("_logger", "_context")

    def __init__(
        self,
        logger: logging.Logger,
        context: Mapping[str, object] | None = None,
    ) -> None:
        self._logger = logger
        self._context: dict[str, object] = dict(context) if context else {}

    @property
    def context(self) -> Mapping[str, object]:
        """The structured context currently bound to this logger."""
        return dict(self._context)

    @property
    def name(self) -> str:
        """The underlying logger's name."""
        return self._logger.name

    def bind(self, **context: object) -> BoundLogger:
        """Return a new logger with ``context`` merged over the current context."""
        merged: dict[str, object] = {**self._context, **context}
        return BoundLogger(self._logger, merged)

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - mirror stdlib API.
        """Whether a message of ``level`` would be processed by this logger."""
        return self._logger.isEnabledFor(level)

    # -- emission -------------------------------------------------------- #

    def _emit(
        self,
        level: int,
        msg: object,
        args: tuple[object, ...],
        *,
        exc_info: _ExcInfo = None,
        stack_info: bool = False,
        stacklevel: int = 1,
    ) -> None:
        if not self._logger.isEnabledFor(level):
            return
        extra = {_CONTEXT_KEY: dict(self._context)} if self._context else None
        # ``stacklevel`` is offset by 2 so reported file/line point at the
        # caller of the public method rather than this wrapper.
        self._logger.log(
            level,
            msg,
            *args,
            exc_info=exc_info,
            stack_info=stack_info,
            stacklevel=stacklevel + 2,
            extra=extra,
        )

    def debug(
        self,
        msg: object,
        *args: object,
        exc_info: _ExcInfo = None,
        stack_info: bool = False,
        stacklevel: int = 1,
    ) -> None:
        self._emit(
            logging.DEBUG, msg, args,
            exc_info=exc_info, stack_info=stack_info, stacklevel=stacklevel,
        )

    def info(
        self,
        msg: object,
        *args: object,
        exc_info: _ExcInfo = None,
        stack_info: bool = False,
        stacklevel: int = 1,
    ) -> None:
        self._emit(
            logging.INFO, msg, args,
            exc_info=exc_info, stack_info=stack_info, stacklevel=stacklevel,
        )

    def warning(
        self,
        msg: object,
        *args: object,
        exc_info: _ExcInfo = None,
        stack_info: bool = False,
        stacklevel: int = 1,
    ) -> None:
        self._emit(
            logging.WARNING, msg, args,
            exc_info=exc_info, stack_info=stack_info, stacklevel=stacklevel,
        )

    def error(
        self,
        msg: object,
        *args: object,
        exc_info: _ExcInfo = None,
        stack_info: bool = False,
        stacklevel: int = 1,
    ) -> None:
        self._emit(
            logging.ERROR, msg, args,
            exc_info=exc_info, stack_info=stack_info, stacklevel=stacklevel,
        )

    def critical(
        self,
        msg: object,
        *args: object,
        exc_info: _ExcInfo = None,
        stack_info: bool = False,
        stacklevel: int = 1,
    ) -> None:
        self._emit(
            logging.CRITICAL, msg, args,
            exc_info=exc_info, stack_info=stack_info, stacklevel=stacklevel,
        )

    def exception(
        self,
        msg: object,
        *args: object,
        exc_info: _ExcInfo = True,
        stack_info: bool = False,
        stacklevel: int = 1,
    ) -> None:
        self._emit(
            logging.ERROR, msg, args,
            exc_info=exc_info, stack_info=stack_info, stacklevel=stacklevel,
        )

    def log(
        self,
        level: int,
        msg: object,
        *args: object,
        exc_info: _ExcInfo = None,
        stack_info: bool = False,
        stacklevel: int = 1,
    ) -> None:
        self._emit(
            level, msg, args,
            exc_info=exc_info, stack_info=stack_info, stacklevel=stacklevel,
        )


def _coerce_level(level: str | int) -> int:
    """Translate a level name or numeric value into a logging level int."""
    if isinstance(level, int):
        return level
    mapping = logging.getLevelNamesMapping()
    resolved = mapping.get(level.upper())
    if resolved is None:
        raise ValueError(f"unknown log level: {level!r}")
    return resolved


def _qualified(name: str) -> str:
    """Ensure ``name`` lives under the package logger namespace."""
    if not name or name == PACKAGE_LOGGER_NAME:
        return PACKAGE_LOGGER_NAME
    if name.startswith(f"{PACKAGE_LOGGER_NAME}."):
        return name
    return f"{PACKAGE_LOGGER_NAME}.{name}"


def configure_logging(level: str | int = "INFO") -> None:
    """Configure structured logging for the application.

    Installs a single resilient stream handler on the package logger with the
    structured formatter and the given severity ``level`` (Req 18.6). Safe to
    call more than once: previously installed handlers on the package logger
    are replaced. Also disables logging's global ``raiseExceptions`` so a sink
    failure can never crash the process (Req 18.5).

    Args:
        level: Severity threshold as a level name (e.g. ``"INFO"``,
            ``"DEBUG"``) or a numeric :mod:`logging` level.
    """
    # A failed log emission must never raise out of the logging machinery.
    logging.raiseExceptions = False

    package_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    package_logger.setLevel(_coerce_level(level))

    # Replace any handlers we may have added on a previous call (idempotent).
    for old_handler in list(package_logger.handlers):
        package_logger.removeHandler(old_handler)

    handler: logging.Handler = _ResilientStreamHandler(stream=sys.stderr)
    handler.setFormatter(_StructuredFormatter(fmt=_DEFAULT_FORMAT, datefmt=_DEFAULT_DATEFMT))
    package_logger.addHandler(handler)

    # Keep package records from also hitting the root logger's handlers.
    package_logger.propagate = False


def get_logger(name: str, **context: object) -> BoundLogger:
    """Return a :class:`BoundLogger` under the package namespace.

    Args:
        name: Logger name, typically ``__name__``. Names outside the package
            namespace are reparented under it so :func:`configure_logging`'s
            handler always applies.
        **context: Optional structured context to bind (e.g.
            ``provider="open-meteo"``, ``subscription_id=42``,
            ``spot_key="es/mundaka"``).

    Returns:
        A logger that renders the bound context with every record.
    """
    logger = logging.getLogger(_qualified(name))
    return BoundLogger(logger, context or None)
