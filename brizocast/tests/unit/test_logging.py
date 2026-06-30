"""Unit tests for the structured, resilient logging setup (Req 18.1, 18.5, 18.6)."""

from __future__ import annotations

import io
import logging
from typing import TextIO

import pytest

from brizocast.core import logging as blog


@pytest.fixture(autouse=True)
def _reset_package_logger() -> None:
    """Ensure each test starts from a clean package-logger state."""
    pkg = logging.getLogger(blog.PACKAGE_LOGGER_NAME)
    for handler in list(pkg.handlers):
        pkg.removeHandler(handler)
    pkg.setLevel(logging.NOTSET)


def _capture_handler() -> tuple[logging.Handler, list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    return _ListHandler(), records


def test_bound_logger_merges_and_renders_context() -> None:
    blog.configure_logging("DEBUG")
    handler, records = _capture_handler()
    logging.getLogger(blog.PACKAGE_LOGGER_NAME).addHandler(handler)

    log = blog.get_logger("svc.forecast", provider="open-meteo").bind(
        subscription_id=42, spot_key="es/mundaka"
    )
    log.info("fetching forecast")

    assert len(records) == 1
    record = records[0]
    context = getattr(record, "context")
    assert context == {
        "provider": "open-meteo",
        "subscription_id": 42,
        "spot_key": "es/mundaka",
    }

    rendered = blog._StructuredFormatter(fmt="%(message)s").format(record)
    assert "fetching forecast" in rendered
    assert "provider=open-meteo" in rendered
    assert "subscription_id=42" in rendered
    assert "spot_key=es/mundaka" in rendered


def test_bind_is_immutable() -> None:
    base = blog.get_logger("svc", provider="a")
    child = base.bind(spot_key="x")
    assert base.context == {"provider": "a"}
    assert child.context == {"provider": "a", "spot_key": "x"}


def test_log_severity_levels_recorded() -> None:
    blog.configure_logging("DEBUG")
    handler, records = _capture_handler()
    logging.getLogger(blog.PACKAGE_LOGGER_NAME).addHandler(handler)

    log = blog.get_logger("svc")
    log.warning("careful")
    log.error("broke")

    assert [r.levelno for r in records] == [logging.WARNING, logging.ERROR]


def test_configure_logging_is_idempotent() -> None:
    blog.configure_logging("INFO")
    blog.configure_logging("DEBUG")
    pkg = logging.getLogger(blog.PACKAGE_LOGGER_NAME)
    assert len(pkg.handlers) == 1
    assert pkg.level == logging.DEBUG


def test_unknown_level_name_rejected() -> None:
    with pytest.raises(ValueError):
        blog.configure_logging("NOPE")


def test_log_write_failure_does_not_raise() -> None:
    """A failure to write a log entry must not crash the caller (Req 18.5)."""

    class _BrokenStream(io.StringIO):
        def write(self, s: str) -> int:
            raise OSError("disk full")

    blog.configure_logging("DEBUG")
    pkg = logging.getLogger(blog.PACKAGE_LOGGER_NAME)
    handler = pkg.handlers[0]
    # Point the configured resilient handler at a stream that always fails.
    broken: TextIO = _BrokenStream()
    setattr(handler, "stream", broken)

    log = blog.get_logger("svc", provider="open-meteo")
    # Should complete without propagating the OSError.
    log.error("this entry cannot be written")
    log.info("nor this one")
