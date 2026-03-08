"""Tests for MetricsCollector and logging constants."""

from core.logging import (
    HEARTBEAT_SENT,
    LEASE_REVOKED,
    POLL_EMPTY,
    REAPER_DEAD_LETTERED,
    REAPER_LEASE_EXPIRED,
    REAPER_TASK_TIMEOUT,
    TASK_CLAIMED,
    TASK_COMPLETED,
    TASK_DEAD_LETTERED,
    TASK_REQUEUED,
    MetricsCollector,
)


class TestMetricsCollector:
    def test_increment_counter(self):
        m = MetricsCollector()
        m.increment("tasks.active")
        assert m.get_counter("tasks.active") == 1
        m.increment("tasks.active", 5)
        assert m.get_counter("tasks.active") == 6

    def test_counter_with_labels(self):
        m = MetricsCollector()
        m.increment("tasks.active", worker_id="w1")
        m.increment("tasks.active", worker_id="w2")
        m.increment("tasks.active", worker_id="w1")

        assert m.get_counter("tasks.active", worker_id="w1") == 2
        assert m.get_counter("tasks.active", worker_id="w2") == 1

    def test_set_gauge(self):
        m = MetricsCollector()
        m.set_gauge("workers.active_tasks", 5, worker_id="w1")
        assert m.get_gauge("workers.active_tasks", worker_id="w1") == 5

        m.set_gauge("workers.active_tasks", 3, worker_id="w1")
        assert m.get_gauge("workers.active_tasks", worker_id="w1") == 3

    def test_gauge_default_zero(self):
        m = MetricsCollector()
        assert m.get_gauge("nonexistent") == 0

    def test_counter_default_zero(self):
        m = MetricsCollector()
        assert m.get_counter("nonexistent") == 0

    def test_queue_depth_gauge(self):
        m = MetricsCollector()
        m.set_gauge("queue.depth", 42)
        assert m.get_gauge("queue.depth") == 42

    def test_poll_empty_counter(self):
        m = MetricsCollector()
        m.increment("poll.empty", worker_id="w1")
        m.increment("poll.empty", worker_id="w1")
        assert m.get_counter("poll.empty", worker_id="w1") == 2

    def test_leases_expired_counter(self):
        m = MetricsCollector()
        m.increment("leases.expired")
        m.increment("leases.expired")
        m.increment("leases.expired")
        assert m.get_counter("leases.expired") == 3


class TestEventConstants:
    """Verify lifecycle event constants are defined."""

    def test_event_names(self):
        assert TASK_CLAIMED == "TASK_CLAIMED"
        assert LEASE_REVOKED == "LEASE_REVOKED"
        assert TASK_DEAD_LETTERED == "TASK_DEAD_LETTERED"
        assert TASK_COMPLETED == "TASK_COMPLETED"
        assert TASK_REQUEUED == "TASK_REQUEUED"
        assert REAPER_LEASE_EXPIRED == "REAPER_LEASE_EXPIRED"
        assert REAPER_TASK_TIMEOUT == "REAPER_TASK_TIMEOUT"
        assert REAPER_DEAD_LETTERED == "REAPER_DEAD_LETTERED"
        assert HEARTBEAT_SENT == "HEARTBEAT_SENT"
        assert POLL_EMPTY == "POLL_EMPTY"
