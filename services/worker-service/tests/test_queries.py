"""Tests verifying SQL queries match the design document exactly."""

from core.heartbeat import HEARTBEAT_QUERY
from core.poller import CLAIM_QUERY
from core.reaper import (
    REAPER_DEAD_LETTER_QUERY,
    REAPER_REQUEUE_QUERY,
    REAPER_TIMEOUT_QUERY,
)


class TestClaimQueryContract:
    """Verify the claim query matches docs/design/PHASE1_DURABLE_EXECUTION.md Section 6.1."""

    def test_uses_cte_pattern(self):
        assert "WITH claimable AS" in CLAIM_QUERY

    def test_selects_queued_tasks(self):
        assert "status = 'queued'" in CLAIM_QUERY

    def test_filters_by_pool(self):
        assert "worker_pool_id = $1" in CLAIM_QUERY

    def test_filters_by_tenant(self):
        assert "tenant_id = $2" in CLAIM_QUERY

    def test_respects_retry_after(self):
        assert "retry_after IS NULL OR retry_after < NOW()" in CLAIM_QUERY

    def test_orders_by_created_at(self):
        assert "ORDER BY created_at" in CLAIM_QUERY

    def test_limits_to_one(self):
        assert "LIMIT 1" in CLAIM_QUERY

    def test_for_update_skip_locked(self):
        assert "FOR UPDATE SKIP LOCKED" in CLAIM_QUERY

    def test_updates_to_running(self):
        assert "SET status = 'running'" in CLAIM_QUERY

    def test_sets_lease_owner(self):
        assert "lease_owner = $3" in CLAIM_QUERY

    def test_sets_lease_expiry_60s(self):
        assert "lease_expiry = NOW() + INTERVAL '60 seconds'" in CLAIM_QUERY

    def test_increments_version(self):
        assert "version = t.version + 1" in CLAIM_QUERY

    def test_returns_full_row(self):
        assert "RETURNING t.*" in CLAIM_QUERY

    def test_no_version_in_where(self):
        """Version check is intentionally omitted from WHERE clause per design doc."""
        # Ensure version is not in the WHERE of the CTE or UPDATE
        where_sections = CLAIM_QUERY.split("WHERE")
        for section in where_sections[1:]:
            # Only check until SET or RETURNING
            end = section.find("SET")
            if end == -1:
                end = section.find("RETURNING")
            if end == -1:
                end = len(section)
            where_clause = section[:end]
            assert "version" not in where_clause.lower() or "t.version + 1" in section


class TestHeartbeatQueryContract:
    """Verify the heartbeat query matches design doc."""

    def test_extends_lease_60s(self):
        assert "lease_expiry = NOW() + INTERVAL '60 seconds'" in HEARTBEAT_QUERY

    def test_checks_task_id(self):
        assert "task_id = $1" in HEARTBEAT_QUERY

    def test_checks_tenant_id(self):
        assert "tenant_id = $2" in HEARTBEAT_QUERY

    def test_checks_lease_owner(self):
        assert "lease_owner = $3" in HEARTBEAT_QUERY

    def test_checks_running_status(self):
        assert "status = 'running'" in HEARTBEAT_QUERY

    def test_no_version_check(self):
        """Heartbeat must NOT check version per design doc."""
        assert "version" not in HEARTBEAT_QUERY.lower()


class TestReaperRequeueQueryContract:
    """Verify the reaper requeue query matches design doc."""

    def test_sets_queued(self):
        assert "status = 'queued'" in REAPER_REQUEUE_QUERY

    def test_clears_lease(self):
        assert "lease_owner = NULL" in REAPER_REQUEUE_QUERY
        assert "lease_expiry = NULL" in REAPER_REQUEUE_QUERY

    def test_increments_retry_count(self):
        assert "retry_count = retry_count + 1" in REAPER_REQUEUE_QUERY

    def test_sets_exponential_backoff(self):
        assert "POWER(2, retry_count)" in REAPER_REQUEUE_QUERY

    def test_appends_retry_history(self):
        assert "retry_history || jsonb_build_array(NOW())" in REAPER_REQUEUE_QUERY

    def test_checks_running_with_expired_lease(self):
        assert "status = 'running'" in REAPER_REQUEUE_QUERY
        assert "lease_expiry < NOW()" in REAPER_REQUEUE_QUERY

    def test_checks_retry_count_lt_max(self):
        assert "retry_count < max_retries" in REAPER_REQUEUE_QUERY

    def test_emits_pg_notify(self):
        assert "pg_notify('new_task', worker_pool_id)" in REAPER_REQUEUE_QUERY


class TestReaperDeadLetterQueryContract:
    def test_sets_dead_letter(self):
        assert "status = 'dead_letter'" in REAPER_DEAD_LETTER_QUERY

    def test_records_last_worker(self):
        assert "last_worker_id = lease_owner" in REAPER_DEAD_LETTER_QUERY

    def test_sets_reason(self):
        assert "dead_letter_reason = 'retries_exhausted'" in REAPER_DEAD_LETTER_QUERY

    def test_checks_retry_count_gte_max(self):
        assert "retry_count >= max_retries" in REAPER_DEAD_LETTER_QUERY


class TestReaperTimeoutQueryContract:
    def test_sets_dead_letter(self):
        assert "status = 'dead_letter'" in REAPER_TIMEOUT_QUERY

    def test_sets_timeout_reason(self):
        assert "dead_letter_reason = 'task_timeout'" in REAPER_TIMEOUT_QUERY

    def test_checks_both_running_and_queued(self):
        assert "status IN ('running', 'queued')" in REAPER_TIMEOUT_QUERY

    def test_checks_task_timeout(self):
        assert "task_timeout_seconds * INTERVAL '1 second'" in REAPER_TIMEOUT_QUERY
