"""Shared test fixtures for worker service tests."""

from __future__ import annotations

import pytest

from core.config import WorkerConfig
from core.logging import MetricsCollector


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
