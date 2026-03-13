"""Tests for WorkerConfig."""

import json
import pytest

from core.config import WorkerConfig, _generate_worker_id


class TestWorkerConfig:
    def test_default_values(self):
        config = WorkerConfig()
        assert config.worker_pool_id == "shared"
        assert config.tenant_id == "default"
        assert config.max_concurrent_tasks == 10
        assert config.poll_backoff_initial_ms == 100
        assert config.poll_backoff_max_ms == 5000
        assert config.poll_backoff_multiplier == 2.0
        assert config.lease_duration_seconds == 60
        assert config.heartbeat_interval_seconds == 15
        assert config.reaper_interval_seconds == 30
        assert config.reaper_jitter_seconds == 10

    def test_worker_id_generated(self):
        config = WorkerConfig()
        assert config.worker_id.startswith("worker-")
        # Should contain hostname, pid, and uuid parts
        parts = config.worker_id.split("-")
        assert len(parts) >= 4

    def test_worker_id_unique(self):
        id1 = _generate_worker_id()
        id2 = _generate_worker_id()
        assert id1 != id2

    def test_frozen(self):
        config = WorkerConfig()
        try:
            config.worker_id = "new-id"  # type: ignore[misc]
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass

    def test_custom_values(self):
        config = WorkerConfig(
            worker_id="custom-worker",
            max_concurrent_tasks=5,
            poll_backoff_initial_ms=200,
        )
        assert config.worker_id == "custom-worker"
        assert config.max_concurrent_tasks == 5
        assert config.poll_backoff_initial_ms == 200

    def test_timing_values_can_be_loaded_from_environment(self, monkeypatch):
        monkeypatch.setenv("LEASE_DURATION_SECONDS", "11")
        monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", "4")
        monkeypatch.setenv("REAPER_INTERVAL_SECONDS", "9")
        monkeypatch.setenv("REAPER_JITTER_SECONDS", "2")

        config = WorkerConfig()

        assert config.lease_duration_seconds == 11
        assert config.heartbeat_interval_seconds == 4
        assert config.reaper_interval_seconds == 9
        assert config.reaper_jitter_seconds == 2

    def test_invalid_timing_environment_value_raises(self, monkeypatch):
        monkeypatch.setenv("LEASE_DURATION_SECONDS", "not-an-int")

        with pytest.raises(ValueError, match="LEASE_DURATION_SECONDS must be an integer"):
            WorkerConfig()
