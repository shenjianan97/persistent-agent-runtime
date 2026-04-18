package com.persistentagent.api.repository;

import com.persistentagent.api.model.response.AttachedMemoryPreview;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

/**
 * Repository for the {@code task_attached_memories} join table.
 *
 * <p>Every query on {@code agent_memory_entries} or {@code task_attached_memories} MUST
 * include both {@code tenant_id} and {@code agent_id} predicates; the track's
 * "memory-query invariant" in the design doc forbids unscoped reads. The scoped
 * {@link #resolveScopedMemoryIds(String, String, List)} method is the canonical
 * resolver used by task submission to validate attachment ids in a single SQL round-trip.
 */
@Repository
public class TaskAttachedMemoryRepository {

    private final JdbcTemplate jdbcTemplate;

    public TaskAttachedMemoryRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    /**
     * Resolves which memory_ids from the given list actually belong to the given
     * {@code (tenant_id, agent_id)} scope. Callers detect validation failures by
     * comparing the returned list size to the input list size — any missing id
     * indicates an unknown id, a wrong-tenant id, or a wrong-agent id. The caller
     * MUST NOT differentiate the cause in its error response (see track design
     * doc's "404-not-403 disclosure rule").
     *
     * <p>Returns an empty list without hitting the database when the input is empty.
     */
    public List<UUID> resolveScopedMemoryIds(String tenantId, String agentId, List<UUID> memoryIds) {
        if (memoryIds == null || memoryIds.isEmpty()) {
            return List.of();
        }

        String placeholders = memoryIds.stream()
                .map(id -> "?::uuid")
                .collect(Collectors.joining(", "));
        String sql = "SELECT memory_id FROM agent_memory_entries"
                + " WHERE memory_id IN (" + placeholders + ")"
                + " AND tenant_id = ? AND agent_id = ?";

        List<Object> args = new ArrayList<>(memoryIds.size() + 2);
        for (UUID id : memoryIds) {
            args.add(id.toString());
        }
        args.add(tenantId);
        args.add(agentId);
        return jdbcTemplate.queryForList(sql, UUID.class, args.toArray());
    }

    /**
     * Inserts one row per memory id into {@code task_attached_memories} with
     * {@code position} preserving input order (0-indexed). No-op for an empty list.
     *
     * <p>Expected to run inside the caller's transaction; the outer
     * {@code @Transactional} rolls back the task row on any failure here.
     */
    public void insertAttachments(UUID taskId, List<UUID> memoryIds) {
        if (memoryIds == null || memoryIds.isEmpty()) {
            return;
        }

        String sql = """
                INSERT INTO task_attached_memories (task_id, memory_id, position)
                VALUES (?, ?, ?)
                """;

        List<Object[]> batchArgs = IntStream.range(0, memoryIds.size())
                .mapToObj(i -> new Object[]{taskId, memoryIds.get(i), i})
                .toList();
        jdbcTemplate.batchUpdate(sql, batchArgs);
    }

    /**
     * Returns all attached memory_ids for a task, ordered by {@code position}
     * (the order they were supplied at submission). Scoped by (tenant, agent) via
     * an inner join on {@code tasks} so the memory-query invariant holds even if
     * a future caller forgets to pre-verify task ownership.
     */
    public List<UUID> findAttachedMemoryIds(UUID taskId, String tenantId, String agentId) {
        String sql = """
                SELECT tam.memory_id
                FROM task_attached_memories tam
                JOIN tasks t
                  ON t.task_id = tam.task_id
                 AND t.tenant_id = ?
                 AND t.agent_id = ?
                WHERE tam.task_id = ?
                ORDER BY tam.position ASC
                """;
        return jdbcTemplate.queryForList(sql, UUID.class, tenantId, agentId, taskId);
    }

    /**
     * Returns human-readable preview rows (memory_id + title) for attachments that
     * still resolve to live {@code agent_memory_entries} within the caller's
     * {@code (tenant_id, agent_id)} scope, ordered by {@code position}. Missing entries
     * (deleted, cross-tenant, cross-agent) are silently omitted — the Console
     * infers their existence by comparing against
     * {@link #findAttachedMemoryIds(UUID)}.
     */
    public List<AttachedMemoryPreview> findAttachedMemoriesPreview(
            UUID taskId, String tenantId, String agentId) {
        String sql = """
                SELECT m.memory_id, m.title
                FROM task_attached_memories tam
                JOIN agent_memory_entries m
                  ON m.memory_id = tam.memory_id
                 AND m.tenant_id = ?
                 AND m.agent_id = ?
                WHERE tam.task_id = ?
                ORDER BY tam.position ASC
                """;

        RowMapper<AttachedMemoryPreview> mapper = (rs, rowNum) -> new AttachedMemoryPreview(
                (UUID) rs.getObject("memory_id"),
                rs.getString("title"));

        return jdbcTemplate.query(sql, mapper, tenantId, agentId, taskId);
    }
}
