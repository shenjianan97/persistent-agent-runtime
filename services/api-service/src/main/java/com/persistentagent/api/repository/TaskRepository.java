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
            UUID langfuseEndpointId, String memoryMode) {
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
                                       langfuse_endpoint_id, agent_display_name_snapshot,
                                       memory_mode)
                    SELECT ?, a.agent_id, a.agent_config, ?,
                           ?, ?, ?, ?, 'queued',
                           ?, a.display_name,
                           ?
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
                memoryMode,
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
                       langfuse_endpoint_id,
                       pending_input_prompt, pending_approval_action, human_input_timeout_at,
                       pause_reason, pause_details, resume_eligible_at
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
                       t.pending_input_prompt, t.pending_approval_action, t.human_input_timeout_at,
                       t.pause_reason, t.pause_details, t.resume_eligible_at,
                       t.memory_mode,
                       (SELECT COALESCE(COUNT(*), 0) FROM checkpoints c WHERE c.task_id = t.task_id AND c.checkpoint_ns = '') AS checkpoint_count,
                       (SELECT COALESCE(SUM(c.cost_microdollars), 0) FROM checkpoints c WHERE c.task_id = t.task_id AND c.checkpoint_ns = '') AS total_cost_microdollars
                FROM tasks t
                WHERE t.task_id = ? AND t.tenant_id = ?
                """;

        List<Map<String, Object>> results = jdbcTemplate.queryForList(sql, taskId, tenantId);
        return results.isEmpty() ? Optional.empty() : Optional.of(results.get(0));
    }

    /**
     * Phase 2 Track 7 Follow-up Task 8 — fetch the latest root-namespace
     * checkpoint row for a task. Used by the Activity projection service
     * to read {@code state["messages"]} without paging the full checkpoint
     * history. Returns {@code Optional.empty()} when the task does not exist
     * for the given tenant, or no root-namespace checkpoint has been written
     * yet. The primary-key index
     * {@code (task_id, checkpoint_ns, created_at)} keeps this query on a
     * single B-tree descent.
     */
    public Optional<Map<String, Object>> getLatestRootCheckpoint(UUID taskId, String tenantId) {
        String checkSql = "SELECT 1 FROM tasks WHERE task_id = ? AND tenant_id = ?";
        if (jdbcTemplate.queryForList(checkSql, taskId, tenantId).isEmpty()) {
            return Optional.empty();
        }
        String sql = """
                SELECT checkpoint_id, checkpoint_payload, created_at
                FROM checkpoints
                WHERE task_id = ? AND checkpoint_ns = ''
                ORDER BY created_at DESC
                LIMIT 1
                """;
        List<Map<String, Object>> rows = jdbcTemplate.queryForList(sql, taskId);
        return rows.isEmpty() ? Optional.empty() : Optional.of(rows.get(0));
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
     * Result of a cancel operation that includes the previous status and agent_id
     * for event recording.
     */
    public record CancelResult(MutationResult outcome, String previousStatus, String agentId) {}

    /**
     * Cancels a task (queued or running -> dead_letter) in a single query.
     * Returns CancelResult with outcome and previous status for audit trail.
     */
    public CancelResult cancelTask(UUID taskId, String tenantId) {
        String sql = """
                WITH target AS (
                    SELECT task_id, status, agent_id FROM tasks WHERE task_id = ? AND tenant_id = ?
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
                        pending_input_prompt = NULL,
                        pending_approval_action = NULL,
                        human_input_timeout_at = NULL,
                        human_response = NULL,
                        version = version + 1,
                        updated_at = NOW()
                    WHERE task_id = ? AND tenant_id = ?
                      AND status IN ('queued', 'running', 'waiting_for_approval', 'waiting_for_input', 'paused')
                    RETURNING task_id
                )
                SELECT
                    (SELECT COUNT(*) FROM target) AS found,
                    (SELECT COUNT(*) FROM updated) AS updated,
                    (SELECT status FROM target LIMIT 1) AS previous_status,
                    (SELECT agent_id FROM target LIMIT 1) AS agent_id
                """;

        Map<String, Object> result = jdbcTemplate.queryForMap(sql, taskId, tenantId, taskId, tenantId);
        long updated = ((Number) result.get("updated")).longValue();
        String previousStatus = (String) result.get("previous_status");
        String agentId = (String) result.get("agent_id");
        if (updated > 0) return new CancelResult(MutationResult.UPDATED, previousStatus, agentId);
        long found = ((Number) result.get("found")).longValue();
        MutationResult outcome = found > 0 ? MutationResult.WRONG_STATE : MutationResult.NOT_FOUND;
        return new CancelResult(outcome, previousStatus, agentId);
    }

    /**
     * Approves a task waiting for approval. Sets human_response with approval JSON,
     * transitions to queued, and returns worker_pool_id + agent_id for notification/event recording.
     */
    public HitlMutationResult approveTask(UUID taskId, String tenantId) {
        String sql = """
                WITH target AS (
                    SELECT task_id FROM tasks WHERE task_id = ? AND tenant_id = ?
                )
                , updated AS (
                    UPDATE tasks
                    SET status = 'queued',
                        human_response = '{"kind":"approval","approved":true}',
                        pending_approval_action = NULL,
                        human_input_timeout_at = NULL,
                        timeout_reference_at = NOW(),
                        lease_owner = NULL,
                        lease_expiry = NULL,
                        version = version + 1,
                        updated_at = NOW()
                    WHERE task_id = ? AND tenant_id = ?
                      AND status = 'waiting_for_approval'
                    RETURNING task_id, worker_pool_id, agent_id
                )
                SELECT
                    (SELECT COUNT(*) FROM target) AS found,
                    (SELECT COUNT(*) FROM updated) AS updated,
                    (SELECT worker_pool_id FROM updated) AS worker_pool_id,
                    (SELECT agent_id FROM updated) AS agent_id
                """;

        Map<String, Object> result = jdbcTemplate.queryForMap(sql, taskId, tenantId, taskId, tenantId);
        long updatedCount = ((Number) result.get("updated")).longValue();
        if (updatedCount > 0) {
            return new HitlMutationResult(MutationResult.UPDATED,
                    (String) result.get("worker_pool_id"), (String) result.get("agent_id"));
        }
        long found = ((Number) result.get("found")).longValue();
        MutationResult mr = found > 0 ? MutationResult.WRONG_STATE : MutationResult.NOT_FOUND;
        return new HitlMutationResult(mr, null, null);
    }

    /**
     * Rejects a task waiting for approval. Sets human_response with rejection JSON including reason,
     * transitions to queued, and returns worker_pool_id + agent_id.
     */
    public HitlMutationResult rejectTask(UUID taskId, String tenantId, String humanResponse) {
        String sql = """
                WITH target AS (
                    SELECT task_id FROM tasks WHERE task_id = ? AND tenant_id = ?
                )
                , updated AS (
                    UPDATE tasks
                    SET status = 'queued',
                        human_response = ?,
                        pending_approval_action = NULL,
                        human_input_timeout_at = NULL,
                        timeout_reference_at = NOW(),
                        lease_owner = NULL,
                        lease_expiry = NULL,
                        version = version + 1,
                        updated_at = NOW()
                    WHERE task_id = ? AND tenant_id = ?
                      AND status = 'waiting_for_approval'
                    RETURNING task_id, worker_pool_id, agent_id
                )
                SELECT
                    (SELECT COUNT(*) FROM target) AS found,
                    (SELECT COUNT(*) FROM updated) AS updated,
                    (SELECT worker_pool_id FROM updated) AS worker_pool_id,
                    (SELECT agent_id FROM updated) AS agent_id
                """;

        Map<String, Object> result = jdbcTemplate.queryForMap(sql, taskId, tenantId, humanResponse, taskId, tenantId);
        long updatedCount = ((Number) result.get("updated")).longValue();
        if (updatedCount > 0) {
            return new HitlMutationResult(MutationResult.UPDATED,
                    (String) result.get("worker_pool_id"), (String) result.get("agent_id"));
        }
        long found = ((Number) result.get("found")).longValue();
        MutationResult mr = found > 0 ? MutationResult.WRONG_STATE : MutationResult.NOT_FOUND;
        return new HitlMutationResult(mr, null, null);
    }

    /**
     * Responds to a task waiting for human input. Sets human_response with input JSON,
     * transitions to queued, and returns worker_pool_id + agent_id.
     */
    public HitlMutationResult respondToTask(UUID taskId, String tenantId, String humanResponse) {
        String sql = """
                WITH target AS (
                    SELECT task_id FROM tasks WHERE task_id = ? AND tenant_id = ?
                )
                , updated AS (
                    UPDATE tasks
                    SET status = 'queued',
                        human_response = ?,
                        pending_input_prompt = NULL,
                        human_input_timeout_at = NULL,
                        timeout_reference_at = NOW(),
                        lease_owner = NULL,
                        lease_expiry = NULL,
                        version = version + 1,
                        updated_at = NOW()
                    WHERE task_id = ? AND tenant_id = ?
                      AND status = 'waiting_for_input'
                    RETURNING task_id, worker_pool_id, agent_id
                )
                SELECT
                    (SELECT COUNT(*) FROM target) AS found,
                    (SELECT COUNT(*) FROM updated) AS updated,
                    (SELECT worker_pool_id FROM updated) AS worker_pool_id,
                    (SELECT agent_id FROM updated) AS agent_id
                """;

        Map<String, Object> result = jdbcTemplate.queryForMap(sql, taskId, tenantId, humanResponse, taskId, tenantId);
        long updatedCount = ((Number) result.get("updated")).longValue();
        if (updatedCount > 0) {
            return new HitlMutationResult(MutationResult.UPDATED,
                    (String) result.get("worker_pool_id"), (String) result.get("agent_id"));
        }
        long found = ((Number) result.get("found")).longValue();
        MutationResult mr = found > 0 ? MutationResult.WRONG_STATE : MutationResult.NOT_FOUND;
        return new HitlMutationResult(mr, null, null);
    }

    /**
     * Follows up a completed task by transitioning it back to queued with a follow-up payload.
     * Clears output so new output is written on re-completion.
     */
    public HitlMutationResult followUpTask(UUID taskId, String tenantId, String humanResponse) {
        String sql = """
                WITH target AS (
                    SELECT task_id FROM tasks WHERE task_id = ?::uuid AND tenant_id = ?
                ),
                updated AS (
                    UPDATE tasks SET
                        status = 'queued',
                        human_response = ?,
                        output = NULL,
                        lease_owner = NULL,
                        lease_expiry = NULL,
                        timeout_reference_at = NOW(),
                        version = version + 1,
                        updated_at = NOW()
                    WHERE task_id = ?::uuid AND tenant_id = ? AND status = 'completed'
                    RETURNING task_id, agent_id, worker_pool_id
                ),
                notified AS (
                    SELECT pg_notify('new_task', u.worker_pool_id)
                    FROM updated u
                )
                SELECT
                    (SELECT COUNT(*) FROM target) AS found,
                    (SELECT COUNT(*) FROM updated) AS updated,
                    (SELECT worker_pool_id FROM updated) AS worker_pool_id,
                    (SELECT agent_id FROM updated) AS agent_id,
                    n.*
                FROM notified n
                RIGHT JOIN (SELECT 1) AS dummy ON true
                """;

        Map<String, Object> result = jdbcTemplate.queryForMap(sql,
                taskId.toString(), tenantId,
                humanResponse,
                taskId.toString(), tenantId);
        long updatedCount = ((Number) result.get("updated")).longValue();
        if (updatedCount > 0) {
            return new HitlMutationResult(MutationResult.UPDATED,
                    (String) result.get("worker_pool_id"),
                    (String) result.get("agent_id"));
        }
        long found = ((Number) result.get("found")).longValue();
        MutationResult mr = found > 0 ? MutationResult.WRONG_STATE : MutationResult.NOT_FOUND;
        return new HitlMutationResult(mr, null, null);
    }

    /**
     * Result of a HITL mutation that also carries the worker_pool_id and agent_id
     * needed for pg_notify and event recording.
     */
    public record HitlMutationResult(MutationResult result, String workerPoolId, String agentId) {
    }

    /**
     * Result of a resume mutation that carries diagnostic fields for differentiated 409 messages.
     */
    public record ResumeMutationResult(
            MutationResult outcome,
            String workerPoolId,
            String agentId,
            String currentStatus,
            Long taskCost,
            Long budgetMax,
            String agentStatus
    ) {}

    /**
     * Resumes a paused task back to queued, validating budget and agent status atomically.
     * Returns ResumeMutationResult with diagnostic fields for differentiated error messages.
     */
    public ResumeMutationResult resumeTask(UUID taskId, String tenantId) {
        String sql = """
                WITH target AS (
                    SELECT t.task_id, t.status, t.worker_pool_id, t.tenant_id, t.agent_id,
                           COALESCE((SELECT SUM(cost_microdollars) FROM agent_cost_ledger WHERE task_id = t.task_id), 0) AS task_cost,
                           a.budget_max_per_task, a.status AS agent_status
                    FROM tasks t
                    JOIN agents a ON t.tenant_id = a.tenant_id AND t.agent_id = a.agent_id
                    WHERE t.task_id = ? AND t.tenant_id = ?
                ),
                updated AS (
                    UPDATE tasks t
                    SET status = 'queued', pause_reason = NULL, pause_details = NULL,
                        resume_eligible_at = NULL, version = version + 1, updated_at = NOW()
                    FROM target tgt
                    WHERE t.task_id = tgt.task_id AND t.status = 'paused'
                      AND tgt.task_cost <= tgt.budget_max_per_task AND tgt.agent_status = 'active'
                    RETURNING t.task_id, tgt.worker_pool_id, tgt.agent_id
                )
                SELECT (SELECT COUNT(*) FROM target) AS found,
                    (SELECT COUNT(*) FROM updated) AS changed,
                    (SELECT status FROM target LIMIT 1) AS current_status,
                    (SELECT task_cost FROM target LIMIT 1) AS task_cost,
                    (SELECT budget_max_per_task FROM target LIMIT 1) AS budget_max,
                    (SELECT agent_status FROM target LIMIT 1) AS agent_status,
                    (SELECT worker_pool_id FROM updated LIMIT 1) AS worker_pool_id,
                    (SELECT agent_id FROM updated LIMIT 1) AS agent_id
                """;

        Map<String, Object> result = jdbcTemplate.queryForMap(sql, taskId, tenantId);
        long found = ((Number) result.get("found")).longValue();
        long changed = ((Number) result.get("changed")).longValue();

        if (found == 0) {
            return new ResumeMutationResult(MutationResult.NOT_FOUND, null, null, null, null, null, null);
        }

        String currentStatus = (String) result.get("current_status");
        Long taskCost = result.get("task_cost") != null ? ((Number) result.get("task_cost")).longValue() : null;
        Long budgetMax = result.get("budget_max") != null ? ((Number) result.get("budget_max")).longValue() : null;
        String agentStatus = (String) result.get("agent_status");

        if (changed > 0) {
            return new ResumeMutationResult(MutationResult.UPDATED,
                    (String) result.get("worker_pool_id"),
                    (String) result.get("agent_id"),
                    currentStatus, taskCost, budgetMax, agentStatus);
        }

        return new ResumeMutationResult(MutationResult.WRONG_STATE,
                null, null, currentStatus, taskCost, budgetMax, agentStatus);
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
     * Result of a redrive operation that includes agent_id for event recording.
     */
    public record RedriveResult(MutationResult outcome, String agentId) {}

    /**
     * Redrives a dead-lettered task back to queued with pg_notify.
     * Returns RedriveResult with outcome and agent_id for audit trail.
     */
    public RedriveResult redriveTask(UUID taskId, String tenantId) {
        String sql = """
                WITH target AS (
                    SELECT task_id, agent_id FROM tasks WHERE task_id = ? AND tenant_id = ?
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
                    (SELECT agent_id FROM target LIMIT 1) AS agent_id,
                    n.*
                FROM notified n
                RIGHT JOIN (SELECT 1) AS dummy ON true
                """;

        Map<String, Object> result = jdbcTemplate.queryForMap(sql, taskId, tenantId, taskId, tenantId);
        long updated = ((Number) result.get("updated")).longValue();
        String agentId = (String) result.get("agent_id");
        if (updated > 0) return new RedriveResult(MutationResult.UPDATED, agentId);
        long found = ((Number) result.get("found")).longValue();
        MutationResult outcome = found > 0 ? MutationResult.WRONG_STATE : MutationResult.NOT_FOUND;
        return new RedriveResult(outcome, agentId);
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
     * Lists tasks with optional status, agent_id, and pause_reason filters, ordered by most recent first.
     */
    @SuppressWarnings("null")
    public List<Map<String, Object>> listTasks(String tenantId, String status, String agentId,
            String pauseReason, int limit) {
        StringBuilder sql = new StringBuilder("""
                SELECT t.task_id, t.agent_id, t.agent_display_name_snapshot, t.status, t.retry_count, t.created_at, t.updated_at,
                       t.langfuse_endpoint_id, t.pause_reason, t.resume_eligible_at,
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
        if (pauseReason != null && !pauseReason.isBlank()) {
            sql.append(" AND t.pause_reason = ?");
            params.add(pauseReason);
        }
        sql.append(" GROUP BY t.task_id, t.agent_id, t.agent_display_name_snapshot, t.status, t.retry_count, t.created_at, t.updated_at, t.langfuse_endpoint_id, t.pause_reason, t.resume_eligible_at");
        sql.append(" ORDER BY t.created_at DESC LIMIT ?");
        params.add(limit);

        return jdbcTemplate.queryForList(sql.toString(), params.toArray());
    }

    /**
     * Sends a pg_notify on the 'new_task' channel to wake up polling workers.
     */
    public void notifyNewTask(String workerPoolId) {
        jdbcTemplate.queryForList("SELECT pg_notify('new_task', ?)", workerPoolId);
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
