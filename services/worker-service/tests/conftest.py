"""Shared test fixtures for worker service tests."""

from __future__ import annotations

import os

import pytest
import structlog

from core.config import WorkerConfig
from core.logging import MetricsCollector, configure_logging


@pytest.fixture(scope="session", autouse=True)
def _worker_log_level_debug_for_tests() -> None:
    """Force ``WORKER_LOG_LEVEL=DEBUG`` for the whole test session.

    Production defaults to INFO via ``core.logging._resolve_level``, which
    wraps the logger with ``make_filtering_bound_logger(INFO)`` — a wrapper
    that drops DEBUG calls *before* any processor runs. ``capture_logs``
    hooks the processor chain, so at INFO it captures nothing from DEBUG
    emissions (notably ``compaction.projection_built``). Setting DEBUG at
    session start lets tests inspect every log line the hook produces,
    without changing production defaults. Must be set BEFORE any test
    imports ``core.logging.configure_logging`` indirectly.
    """
    os.environ.setdefault("WORKER_LOG_LEVEL", "DEBUG")
    # Reconfigure in case another import chain already called
    # ``configure_logging()`` with the INFO default.
    structlog.reset_defaults()
    configure_logging()


@pytest.fixture
def config() -> WorkerConfig:
    """A default WorkerConfig for testing."""
    return WorkerConfig(
        worker_id="test-worker-001",
        worker_pool_id="shared",
        tenant_id="default",
        db_dsn="postgresql://localhost:5432/test_agent_runtime",
        max_concurrent_tasks=10,
        poll_backoff_initial_ms=100,
        poll_backoff_max_ms=5000,
        poll_backoff_multiplier=2.0,
        lease_duration_seconds=60,
        heartbeat_interval_seconds=15,
        reaper_interval_seconds=30,
        reaper_jitter_seconds=10,
    )


@pytest.fixture
def metrics() -> MetricsCollector:
    """A fresh MetricsCollector for testing."""
    return MetricsCollector()
