package com.persistentagent.api.repository;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

@Repository
public class TaskRepository {

    private final JdbcTemplate jdbcTemplate;

    public TaskRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    /**
     * Inserts a new task and emits pg_notify for worker notification.
     * Returns the generated task_id and created_at.
     */
    public Map<String, Object> insertTask(String tenantId, String agentId, String agentConfigJson,
            String workerPoolId, String input,
            int maxRetries, int maxSteps, int taskTimeoutSeconds, UUID langfuseEndpointId) {
        String sql = """
                WITH inserted AS (
                    INSERT INTO tasks (tenant_id, agent_id, agent_config_snapshot, worker_pool_id,
                                       input, max_retries, max_steps, task_timeout_seconds, status, langfuse_endpoint_id)
                    VALUES (?, ?, ?::jsonb, ?, ?, ?, ?, ?, 'queued', ?)
                    RETURNING task_id, created_at
                )
                , notified AS (
                    SELECT pg_notify('new_task', ?)
                )
                SELECT i.task_id, i.created_at FROM inserted i, notified n
                """;

        return jdbcTemplate.queryForMap(sql,
                tenantId, agentId, agentConfigJson, workerPoolId,
                input, maxRetries, maxSteps, taskTimeoutSeconds, langfuseEndpointId,
                workerPoolId);
    }

    /**
     * Atomically resolves an agent, validates its model is active, and inserts a new task.
     * Uses INSERT...SELECT with a models JOIN to enforce in a single SQL statement:
     * 1. Agent exists and status = 'active'
     * 2. Agent's model is still active in the models registry
     *
     * Returns Optional.empty() if any condition fails (caller differentiates the reason).
     * Only fires pg_notify when a row is actually inserted.
     */
    public Optional<Map<String, Object>> insertTaskFromAgent(
            String tenantId, String agentId, String workerPoolId,
            String input, int maxRetries, int maxSteps, int taskTimeoutSeconds,
            UUID langfuseEndpointId) {
        String sql = """
                WITH agent AS (
                    SELECT a.agent_id, a.display_name, a.agent_config
                    FROM agents a
                    JOIN models m
                      ON m.provider_id = a.agent_config->>'provider'
                     AND m.model_id   = a.agent_config->>'model'
                     AND m.is_active  = true
                    WHERE a.tenant_id = ? AND a.agent_id = ? AND a.status = 'active'
                ),
                inserted AS (
                    INSERT INTO tasks (tenant_id, agent_id, agent_config_snapshot, worker_pool_id,
                                       input, max_retries, max_steps, task_timeout_seconds, status,
                                       langfuse_endpoint_id, agent_display_name_snapshot)
                    SELECT ?, a.agent_id, a.agent_config, ?,
                           ?, ?, ?, ?, 'queued',
                           ?, a.display_name
                    FROM agent a
                    RETURNING task_id, agent_display_name_snapshot, created_at
                ),
                notified AS (
                    SELECT pg_notify('new_task', ?)
                    FROM inserted
                )
                SELECT i.task_id, i.agent_display_name_snapshot, i.created_at
                FROM inserted i
                LEFT JOIN notified n ON true
                """;
        List<Map<String, Object>> results = jdbcTemplate.queryForList(sql,
                tenantId, agentId,
                tenantId, workerPoolId,
                input, maxRetries, maxSteps, taskTimeoutSeconds,
                langfuseEndpointId,
                workerPoolId);
        return results.isEmpty() ? Optional.empty() : Optional.of(results.get(0));
    }

    /**
     * Finds a task by ID scoped to tenant.
     */
    public Optional<Map<String, Object>> findByIdAndTenant(UUID taskId, String tenantId) {
        String sql = """
                SELECT task_id, tenant_id, agent_id, agent_display_name_snapshot, status, input, output,
                       retry_count, retry_history, lease_owner,
                       last_error_code, last_error_message, last_worker_id,
                       dead_letter_reason, dead_lettered_at, created_at, updated_at,
                       langfuse_endpoint_id
                FROM tasks
                WHERE task_id = ? AND tenant_id = ?
                """;

        List<Map<String, Object>> results = jdbcTemplate.queryForList(sql, taskId, tenantId);
        return results.isEmpty() ? Optional.empty() : Optional.of(results.get(0));
    }

    /**
     * Finds a task by ID scoped to tenant, including checkpoint counts.
     * Cost totals are resolved from the observability service in TaskService.
     */
    public Optional<Map<String, Object>> findByIdWithAggregates(UUID taskId, String tenantId) {
        String sql = """
                SELECT t.task_id, t.tenant_id, t.agent_id, t.agent_display_name_snapshot, t.status, t.input, t.output,
                       t.retry_count, t.retry_history, t.lease_owner,
                       t.last_error_code, t.last_error_message, t.last_worker_id,
                       t.dead_letter_reason, t.dead_lettered_at, t.created_at, t.updated_at,
                       t.langfuse_endpoint_id,
                       (SELECT COALESCE(COUNT(*), 0) FROM checkpoints c WHERE c.task_id = t.task_id AND c.checkpoint_ns = '') AS checkpoint_count,
                       (SELECT COALESCE(SUM(c.cost_microdollars), 0) FROM checkpoints c WHERE c.task_id = t.task_id AND c.checkpoint_ns = '') AS total_cost_microdollars
                FROM tasks t
                WHERE t.task_id = ? AND t.tenant_id = ?
                """;

        List<Map<String, Object>> results = jdbcTemplate.queryForList(sql, taskId, tenantId);
        return results.isEmpty() ? Optional.empty() : Optional.of(results.get(0));
    }

    /**
     * Gets root-namespace checkpoints ordered by creation time.
     * Returns Optional.empty() if the task does not exist for the given tenant.
     */
    public Optional<List<Map<String, Object>>> getCheckpoints(UUID taskId, String tenantId) {
        // First verify task exists and belongs to tenant
        String checkSql = "SELECT 1 FROM tasks WHERE task_id = ? AND tenant_id = ?";
        List<Map<String, Object>> check = jdbcTemplate.queryForList(checkSql, taskId, tenantId);
        if (check.isEmpty()) {
            return Optional.empty();
        }

        String sql = """
                SELECT checkpoint_id, worker_id, cost_microdollars,
                       execution_metadata, metadata_payload, checkpoint_payload, created_at
                FROM checkpoints
                WHERE task_id = ? AND checkpoint_ns = ''
                ORDER BY created_at ASC
                """;
        return Optional.of(jdbcTemplate.queryForList(sql, taskId));
    }

    /**
     * Result of a state-transition operation that distinguishes
     * "task not found" from "task found but in wrong state."
     */
    public enum MutationResult { UPDATED, WRONG_STATE, NOT_FOUND }

    /**
     * Cancels a task (queued or running -> dead_letter) in a single query.
     * Returns UPDATED, WRONG_STATE, or NOT_FOUND.
     */
    public MutationResult cancelTask(UUID taskId, String tenantId) {
        String sql = """
                WITH target AS (
                    SELECT task_id FROM tasks WHERE task_id = ? AND tenant_id = ?
                )
                , updated AS (
                    UPDATE tasks
                    SET status = 'dead_letter',
                        last_worker_id = lease_owner,
                        lease_owner = NULL,
                        lease_expiry = NULL,
                        last_error_code = 'cancelled_by_user',
                        last_error_message = 'task cancelled by user request',
                        dead_letter_reason = 'cancelled_by_user',
                        dead_lettered_at = NOW(),
                        version = version + 1,
                        updated_at = NOW()
                    WHERE task_id = ? AND tenant_id = ?
                      AND status IN ('queued', 'running')
                    RETURNING task_id
                )
                SELECT
                    (SELECT COUNT(*) FROM target) AS found,
                    (SELECT COUNT(*) FROM updated) AS updated
                """;

        Map<String, Object> result = jdbcTemplate.queryForMap(sql, taskId, tenantId, taskId, tenantId);
        long updated = ((Number) result.get("updated")).longValue();
        if (updated > 0) return MutationResult.UPDATED;
        long found = ((Number) result.get("found")).longValue();
        return found > 0 ? MutationResult.WRONG_STATE : MutationResult.NOT_FOUND;
    }

    /**
     * Lists dead-lettered tasks with optional agent_id filter.
     */
    @SuppressWarnings("null")
    public List<Map<String, Object>> listDeadLetterTasks(String tenantId, String agentId, int limit) {
        StringBuilder sql = new StringBuilder("""
                SELECT task_id, agent_id, agent_display_name_snapshot, dead_letter_reason, last_error_code,
                       last_error_message, retry_count, last_worker_id, dead_lettered_at
                FROM tasks
                WHERE tenant_id = ? AND status = 'dead_letter'
                """);
        List<Object> params = new java.util.ArrayList<>();
        params.add(tenantId);

        if (agentId != null && !agentId.isBlank()) {
            sql.append(" AND agent_id = ?");
            params.add(agentId);
        }
        sql.append(" ORDER BY dead_lettered_at DESC, task_id DESC LIMIT ?");
        params.add(limit);

        return jdbcTemplate.queryForList(sql.toString(), params.toArray());
    }

    /**
     * Redrives a dead-lettered task back to queued with pg_notify.
     * Returns UPDATED, WRONG_STATE, or NOT_FOUND.
     */
    public MutationResult redriveTask(UUID taskId, String tenantId) {
        String sql = """
                WITH target AS (
                    SELECT task_id FROM tasks WHERE task_id = ? AND tenant_id = ?
                )
                , redriven AS (
                    UPDATE tasks
                    SET status = 'queued',
                        retry_count = 0,
                        retry_after = NULL,
                        lease_owner = NULL,
                        lease_expiry = NULL,
                        last_error_code = NULL,
                        last_error_message = NULL,
                        last_worker_id = NULL,
                        dead_letter_reason = NULL,
                        dead_lettered_at = NULL,
                        timeout_reference_at = NOW(),
                        version = version + 1,
                        updated_at = NOW()
                    WHERE task_id = ? AND tenant_id = ?
                      AND status = 'dead_letter'
                    RETURNING task_id, worker_pool_id
                )
                , notified AS (
                    SELECT pg_notify('new_task', r.worker_pool_id)
                    FROM redriven r
                )
                SELECT
                    (SELECT COUNT(*) FROM target) AS found,
                    (SELECT COUNT(*) FROM redriven) AS updated,
                    n.*
                FROM notified n
                RIGHT JOIN (SELECT 1) AS dummy ON true
                """;

        Map<String, Object> result = jdbcTemplate.queryForMap(sql, taskId, tenantId, taskId, tenantId);
        long updated = ((Number) result.get("updated")).longValue();
        if (updated > 0) return MutationResult.UPDATED;
        long found = ((Number) result.get("found")).longValue();
        return found > 0 ? MutationResult.WRONG_STATE : MutationResult.NOT_FOUND;
    }

    public boolean expireLease(UUID taskId, String tenantId, String leaseOwnerOverride) {
        String sql = """
                UPDATE tasks
                SET lease_owner = COALESCE(?, lease_owner),
                    lease_expiry = NOW() - INTERVAL '1 second',
                    version = version + 1,
                    updated_at = NOW()
                WHERE task_id = ? AND tenant_id = ?
                  AND status = 'running'
                  AND lease_owner IS NOT NULL
                RETURNING task_id
                """;

        List<Map<String, Object>> results = jdbcTemplate.queryForList(sql, leaseOwnerOverride, taskId, tenantId);
        return !results.isEmpty();
    }

    public boolean forceDeadLetter(
            UUID taskId,
            String tenantId,
            String reason,
            String errorCode,
            String errorMessage,
            String lastWorkerId
    ) {
        String sql = """
                UPDATE tasks
                SET status = 'dead_letter',
                    last_worker_id = COALESCE(?, lease_owner, last_worker_id),
                    lease_owner = NULL,
                    lease_expiry = NULL,
                    last_error_code = ?,
                    last_error_message = ?,
                    dead_letter_reason = ?,
                    dead_lettered_at = NOW(),
                    version = version + 1,
                    updated_at = NOW()
                WHERE task_id = ? AND tenant_id = ?
                  AND status IN ('queued', 'running')
                RETURNING task_id
                """;

        List<Map<String, Object>> results = jdbcTemplate.queryForList(
                sql,
                lastWorkerId,
                errorCode,
                errorMessage,
                reason,
                taskId,
                tenantId
        );
        return !results.isEmpty();
    }

    /**
     * Lists tasks with optional status and agent_id filters, ordered by most recent first.
     */
    @SuppressWarnings("null")
    public List<Map<String, Object>> listTasks(String tenantId, String status, String agentId, int limit) {
        StringBuilder sql = new StringBuilder("""
                SELECT t.task_id, t.agent_id, t.agent_display_name_snapshot, t.status, t.retry_count, t.created_at, t.updated_at,
                       t.langfuse_endpoint_id,
                       COALESCE(COUNT(c.checkpoint_id), 0) AS checkpoint_count,
                       COALESCE(SUM(c.cost_microdollars), 0) AS total_cost_microdollars
                FROM tasks t
                LEFT JOIN checkpoints c ON c.task_id = t.task_id AND c.checkpoint_ns = ''
                WHERE t.tenant_id = ?
                """);
        List<Object> params = new java.util.ArrayList<>();
        params.add(tenantId);

        if (status != null && !status.isBlank()) {
            sql.append(" AND t.status = ?");
            params.add(status);
        }
        if (agentId != null && !agentId.isBlank()) {
            sql.append(" AND t.agent_id = ?");
            params.add(agentId);
        }
        sql.append(" GROUP BY t.task_id, t.agent_id, t.agent_display_name_snapshot, t.status, t.retry_count, t.created_at, t.updated_at, t.langfuse_endpoint_id");
        sql.append(" ORDER BY t.created_at DESC LIMIT ?");
        params.add(limit);

        return jdbcTemplate.queryForList(sql.toString(), params.toArray());
    }

    /**
     * Checks database connectivity.
     */
    public boolean isDatabaseConnected() {
        try {
            jdbcTemplate.queryForObject("SELECT 1", Integer.class);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * Gets count of online workers from the workers registry table.
     * A worker is considered online if its status is 'online' and it has
     * sent a heartbeat within the last 60 seconds.
     */
    public int getActiveWorkerCount() {
        try {
            Integer count = jdbcTemplate.queryForObject(
                    "SELECT COUNT(*) FROM workers WHERE status = 'online' AND last_heartbeat_at > NOW() - INTERVAL '60 seconds'",
                    Integer.class);
            return count != null ? count : 0;
        } catch (Exception e) {
            return 0;
        }
    }

    /**
     * Gets count of queued tasks.
     */
    public int getQueuedTaskCount() {
        try {
            Integer count = jdbcTemplate.queryForObject(
                    "SELECT COUNT(*) FROM tasks WHERE status = 'queued'",
                    Integer.class);
            return count != null ? count : 0;
        } catch (Exception e) {
            return 0;
        }
    }
}
