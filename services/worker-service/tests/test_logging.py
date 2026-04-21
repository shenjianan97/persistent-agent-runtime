"""Tests for ``core.logging`` structlog level plumbing.

Covers the ``WORKER_LOG_LEVEL`` env var introduced for local-dev observability
(``docs/LOCAL_DEVELOPMENT.md`` § Tracking a running task). Production stays at
INFO; DEBUG is strictly opt-in.
"""

from __future__ import annotations

import io
import logging
import sys

import pytest
import structlog

from core.logging import _resolve_level, configure_logging, get_logger


@pytest.fixture(autouse=True)
def _restore_structlog_config():
    """Tests here reconfigure structlog (via ``reset_defaults`` +
    ``configure_logging``) to verify the ``WORKER_LOG_LEVEL`` filter plumbing.
    Restore the session-wide DEBUG setup at teardown so sibling test files
    using ``capture_logs`` aren't left with an INFO-filtered logger that
    silently drops DEBUG emissions (see ``conftest.py`` fixture).
    """
    yield
    import os
    os.environ["WORKER_LOG_LEVEL"] = "DEBUG"
    structlog.reset_defaults()
    configure_logging()


def test_resolve_level_default_is_info(monkeypatch) -> None:
    monkeypatch.delenv("WORKER_LOG_LEVEL", raising=False)
    assert _resolve_level() == logging.INFO


def test_resolve_level_debug(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_LOG_LEVEL", "DEBUG")
    assert _resolve_level() == logging.DEBUG


def test_resolve_level_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_LOG_LEVEL", "warning")
    assert _resolve_level() == logging.WARNING


def test_resolve_level_invalid_falls_back_to_info(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_LOG_LEVEL", "BOGUS")
    assert _resolve_level() == logging.INFO


def test_resolve_level_empty_string_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_LOG_LEVEL", "")
    assert _resolve_level() == logging.INFO


def test_configure_logging_debug_enables_debug_output(monkeypatch) -> None:
    """End-to-end: WORKER_LOG_LEVEL=DEBUG + configure_logging + emit DEBUG → visible."""
    monkeypatch.setenv("WORKER_LOG_LEVEL", "DEBUG")
    # Redirect structlog's PrintLoggerFactory output so we can inspect emitted JSON.
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    # Force structlog to re-resolve the level (bypass cache_logger_on_first_use).
    structlog.reset_defaults()
    configure_logging()
    logger = get_logger(worker_id="test")
    logger.debug("debug_event_visible", foo="bar")
    output = buf.getvalue()
    assert "debug_event_visible" in output, f"DEBUG line not captured: {output!r}"


def test_configure_logging_default_suppresses_debug(monkeypatch) -> None:
    """Default INFO filter drops DEBUG lines (production-equivalent behaviour)."""
    monkeypatch.delenv("WORKER_LOG_LEVEL", raising=False)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    structlog.reset_defaults()
    configure_logging()
    logger = get_logger(worker_id="test")
    logger.debug("debug_event_should_not_appear")
    logger.info("info_event_should_appear")
    output = buf.getvalue()
    assert "debug_event_should_not_appear" not in output
    assert "info_event_should_appear" in output
