"""Tests verifying SQL queries match the design document exactly."""

from core.heartbeat import build_heartbeat_query
from core.poller import (
    _PRECLAIM_UPSERT_SQL,
    _FIND_ELIGIBLE_AGENT_SQL,
    _FIND_AGENT_TASK_SQL,
    _CLAIM_TASK_SQL,
    _UPDATE_RUNTIME_STATE_SQL,
    _INSERT_TASK_EVENT_SQL,
)
from core.reaper import (
    REAPER_DEAD_LETTER_QUERY,
    REAPER_REQUEUE_QUERY,
    REAPER_TIMEOUT_QUERY,
)


class TestClaimQueryContract:
    """Verify the agent-aware round-robin claim SQL matches the Track 3 design contract.

    The claim path uses sequential queries within a single transaction instead
    of a single CTE-based query.  Each SQL fragment is tested individually.
    """

    # --- Pre-claim upsert (Step 0) ---

    def test_preclaim_upsert_targets_agent_runtime_state(self):
        assert "agent_runtime_state" in _PRECLAIM_UPSERT_SQL

    def test_preclaim_upsert_selects_queued_tasks(self):
        assert "status = 'queued'" in _PRECLAIM_UPSERT_SQL

    def test_preclaim_upsert_filters_by_pool(self):
        assert "worker_pool_id = $1" in _PRECLAIM_UPSERT_SQL

    def test_preclaim_upsert_filters_by_tenant(self):
        assert "tenant_id = $2" in _PRECLAIM_UPSERT_SQL

    def test_preclaim_upsert_uses_on_conflict_do_nothing(self):
        assert "ON CONFLICT DO NOTHING" in _PRECLAIM_UPSERT_SQL

    # --- Eligible agent selection (Step 1) ---

    def test_eligible_agent_joins_agents_table(self):
        assert "JOIN agents a" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_checks_active_status(self):
        assert "a.status = 'active'" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_checks_concurrency(self):
        assert "running_task_count < a.max_concurrent_tasks" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_checks_hourly_budget(self):
        assert "hour_window_cost_microdollars < a.budget_max_per_hour" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_has_queued_tasks(self):
        assert "EXISTS" in _FIND_ELIGIBLE_AGENT_SQL
        assert "status = 'queued'" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_respects_retry_after(self):
        assert "t.retry_after IS NULL OR t.retry_after < NOW()" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_orders_by_scheduler_cursor(self):
        assert "ORDER BY ars.scheduler_cursor ASC" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_limits_to_one(self):
        assert "LIMIT 1" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_locks_runtime_state(self):
        assert "FOR UPDATE OF ars" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_filters_by_pool(self):
        assert "worker_pool_id = $1" in _FIND_ELIGIBLE_AGENT_SQL

    def test_eligible_agent_filters_by_tenant(self):
        assert "ars.tenant_id = $2" in _FIND_ELIGIBLE_AGENT_SQL

    # --- Task selection (Step 2) ---

    def test_find_task_selects_queued(self):
        assert "status = 'queued'" in _FIND_AGENT_TASK_SQL

    def test_find_task_filters_by_agent(self):
        assert "agent_id = $2" in _FIND_AGENT_TASK_SQL

    def test_find_task_filters_by_pool(self):
        assert "worker_pool_id = $3" in _FIND_AGENT_TASK_SQL

    def test_find_task_respects_retry_after(self):
        assert "retry_after IS NULL OR retry_after < NOW()" in _FIND_AGENT_TASK_SQL

    def test_find_task_orders_by_created_at(self):
        assert "ORDER BY created_at ASC" in _FIND_AGENT_TASK_SQL

    def test_find_task_limits_to_one(self):
        assert "LIMIT 1" in _FIND_AGENT_TASK_SQL

    def test_find_task_for_update_skip_locked(self):
        assert "FOR UPDATE SKIP LOCKED" in _FIND_AGENT_TASK_SQL

    # --- Claim update (Step 3) ---

    def test_claim_updates_to_running(self):
        assert "status = 'running'" in _CLAIM_TASK_SQL

    def test_claim_sets_lease_owner(self):
        assert "lease_owner = $1" in _CLAIM_TASK_SQL

    def test_claim_sets_lease_expiry(self):
        assert "lease_expiry" in _CLAIM_TASK_SQL

    def test_claim_increments_version(self):
        assert "version = version + 1" in _CLAIM_TASK_SQL

    def test_claim_returns_full_row(self):
        assert "RETURNING *" in _CLAIM_TASK_SQL

    # --- Runtime state update (Step 4) ---

    def test_runtime_state_increments_running_count(self):
        assert "running_task_count = running_task_count + 1" in _UPDATE_RUNTIME_STATE_SQL

    def test_runtime_state_advances_scheduler_cursor(self):
        assert "scheduler_cursor = NOW()" in _UPDATE_RUNTIME_STATE_SQL

    # --- Task event insert (Step 5) ---

    def test_event_insert_targets_task_events(self):
        assert "task_events" in _INSERT_TASK_EVENT_SQL

    def test_event_insert_includes_event_type(self):
        assert "event_type" in _INSERT_TASK_EVENT_SQL


class TestHeartbeatQueryContract:
    """Verify the heartbeat query matches design doc."""

    def test_extends_lease_60s(self):
        assert "lease_expiry = NOW() + INTERVAL '60 seconds'" in build_heartbeat_query(60)

    def test_heartbeat_query_respects_configured_lease_duration(self):
        assert "lease_expiry = NOW() + INTERVAL '7 seconds'" in build_heartbeat_query(7)

    def test_checks_task_id(self):
        assert "task_id = $1" in build_heartbeat_query(60)

    def test_checks_tenant_id(self):
        assert "tenant_id = $2" in build_heartbeat_query(60)

    def test_checks_lease_owner(self):
        assert "lease_owner = $3" in build_heartbeat_query(60)

    def test_checks_running_status(self):
        assert "status = 'running'" in build_heartbeat_query(60)

    def test_no_version_check(self):
        """Heartbeat must NOT check version per design doc."""
        assert "version" not in build_heartbeat_query(60).lower()


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

    def test_uses_timeout_reference_at_not_created_at(self):
        """Reaper must use timeout_reference_at so redriven tasks get a fresh window.

        Issue #13: using created_at caused redriven tasks to be immediately
        dead-lettered again because created_at is never reset on redrive.
        """
        assert "timeout_reference_at" in REAPER_TIMEOUT_QUERY
        assert "created_at" not in REAPER_TIMEOUT_QUERY
