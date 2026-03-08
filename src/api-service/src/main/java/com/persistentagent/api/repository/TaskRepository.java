package com.persistentagent.api.repository;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

@Repository
public class TaskRepository {

    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;

    public TaskRepository(JdbcTemplate jdbcTemplate, ObjectMapper objectMapper) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
    }

    /**
     * Inserts a new task and emits pg_notify for worker notification.
     * Returns the generated task_id and created_at.
     */
    public Map<String, Object> insertTask(String tenantId, String agentId, String agentConfigJson,
                                           String workerPoolId, String input,
                                           int maxRetries, int maxSteps, int taskTimeoutSeconds) {
        String sql = """
                WITH inserted AS (
                    INSERT INTO tasks (tenant_id, agent_id, agent_config_snapshot, worker_pool_id,
                                       input, max_retries, max_steps, task_timeout_seconds, status)
                    VALUES (?, ?, ?::jsonb, ?, ?, ?, ?, ?, 'queued')
                    RETURNING task_id, created_at
                )
                , notified AS (
                    SELECT pg_notify('new_task', ?)
                )
                SELECT task_id, created_at FROM inserted
                """;

        return jdbcTemplate.queryForMap(sql,
                tenantId, agentId, agentConfigJson, workerPoolId,
                input, maxRetries, maxSteps, taskTimeoutSeconds,
                workerPoolId);
    }

    /**
     * Finds a task by ID scoped to tenant.
     */
    public Optional<Map<String, Object>> findByIdAndTenant(UUID taskId, String tenantId) {
        String sql = """
                SELECT task_id, tenant_id, agent_id, status, input, output,
                       retry_count, retry_history, lease_owner,
                       last_error_code, last_error_message, last_worker_id,
                       dead_letter_reason, dead_lettered_at, created_at, updated_at
                FROM tasks
                WHERE task_id = ? AND tenant_id = ?
                """;

        List<Map<String, Object>> results = jdbcTemplate.queryForList(sql, taskId, tenantId);
        return results.isEmpty() ? Optional.empty() : Optional.of(results.get(0));
    }

    /**
     * Gets checkpoint count and total cost for a task in root namespace.
     */
    public Map<String, Object> getCheckpointAggregates(UUID taskId) {
        String sql = """
                SELECT COALESCE(COUNT(*), 0) AS checkpoint_count,
                       COALESCE(SUM(cost_microdollars), 0) AS total_cost_microdollars
                FROM checkpoints
                WHERE task_id = ? AND checkpoint_ns = ''
                """;
        return jdbcTemplate.queryForMap(sql, taskId);
    }

    /**
     * Gets root-namespace checkpoints ordered by creation time.
     */
    public List<Map<String, Object>> getCheckpoints(UUID taskId, String tenantId) {
        // First verify task exists and belongs to tenant
        String checkSql = "SELECT 1 FROM tasks WHERE task_id = ? AND tenant_id = ?";
        List<Map<String, Object>> check = jdbcTemplate.queryForList(checkSql, taskId, tenantId);
        if (check.isEmpty()) {
            return null; // signals not found
        }

        String sql = """
                SELECT checkpoint_id, worker_id, cost_microdollars,
                       execution_metadata, metadata_payload, created_at
                FROM checkpoints
                WHERE task_id = ? AND checkpoint_ns = ''
                ORDER BY created_at ASC
                """;
        return jdbcTemplate.queryForList(sql, taskId);
    }

    /**
     * Cancels a task (queued or running -> dead_letter).
     * Returns number of rows affected.
     */
    public int cancelTask(UUID taskId, String tenantId) {
        String sql = """
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
                """;
        return jdbcTemplate.update(sql, taskId, tenantId);
    }

    /**
     * Lists dead-lettered tasks with optional agent_id filter.
     */
    public List<Map<String, Object>> listDeadLetterTasks(String tenantId, String agentId, int limit) {
        if (agentId != null && !agentId.isBlank()) {
            String sql = """
                    SELECT task_id, agent_id, dead_letter_reason, last_error_code,
                           last_error_message, retry_count, last_worker_id, dead_lettered_at
                    FROM tasks
                    WHERE tenant_id = ? AND agent_id = ? AND status = 'dead_letter'
                    ORDER BY dead_lettered_at DESC, task_id DESC
                    LIMIT ?
                    """;
            return jdbcTemplate.queryForList(sql, tenantId, agentId, limit);
        } else {
            String sql = """
                    SELECT task_id, agent_id, dead_letter_reason, last_error_code,
                           last_error_message, retry_count, last_worker_id, dead_lettered_at
                    FROM tasks
                    WHERE tenant_id = ? AND status = 'dead_letter'
                    ORDER BY dead_lettered_at DESC, task_id DESC
                    LIMIT ?
                    """;
            return jdbcTemplate.queryForList(sql, tenantId, limit);
        }
    }

    /**
     * Redrives a dead-lettered task back to queued with pg_notify.
     * Returns the task_id if redrive succeeded, empty otherwise.
     */
    public Optional<UUID> redriveTask(UUID taskId, String tenantId) {
        String sql = """
                WITH redriven AS (
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
                        version = version + 1,
                        updated_at = NOW()
                    WHERE task_id = ? AND tenant_id = ?
                      AND status = 'dead_letter'
                    RETURNING task_id, worker_pool_id
                )
                , notified AS (
                    SELECT pg_notify('new_task', worker_pool_id)
                    FROM redriven
                )
                SELECT task_id FROM redriven
                """;

        List<Map<String, Object>> results = jdbcTemplate.queryForList(sql, taskId, tenantId);
        if (results.isEmpty()) {
            return Optional.empty();
        }
        return Optional.of((UUID) results.get(0).get("task_id"));
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
     * Gets count of distinct active workers (tasks with status='running' and lease_owner not null).
     */
    public int getActiveWorkerCount() {
        try {
            Integer count = jdbcTemplate.queryForObject(
                    "SELECT COUNT(DISTINCT lease_owner) FROM tasks WHERE status = 'running' AND lease_owner IS NOT NULL",
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
