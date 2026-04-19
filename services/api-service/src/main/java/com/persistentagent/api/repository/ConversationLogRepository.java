package com.persistentagent.api.repository;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.persistentagent.api.model.response.ConversationEntryResponse;
import com.persistentagent.api.util.DateTimeUtil;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.UUID;

/**
 * Phase 2 Track 7 Task 13 — read-only view over the append-only
 * {@code task_conversation_log} table.
 *
 * <p><b>Ownership split:</b> the Python worker owns the write path
 * ({@code append_entry} only). This Java repository is the read path — it
 * reads Postgres directly, no cross-service RPC. Per the Task 13 spec §API
 * read path, this class MUST NOT expose any mutation methods.
 *
 * <p><b>Tenant isolation contract:</b> every query filters by
 * {@code (tenant_id, task_id)}. The caller resolves {@code tenantId} from the
 * authenticated principal — NEVER from a client-supplied parameter. 404 (not
 * 403) when the {@code (task_id, tenant_id)} row does not exist; this
 * prevents task-id enumeration oracles across tenants.
 */
@Repository
public class ConversationLogRepository {

    private static final Logger log = LoggerFactory.getLogger(ConversationLogRepository.class);

    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;

    public ConversationLogRepository(JdbcTemplate jdbcTemplate, ObjectMapper objectMapper) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
    }

    /**
     * Returns conversation-log entries for a task, in ascending {@code sequence}
     * order, starting strictly after {@code afterSequence}.
     *
     * <p>The SQL predicate is authoritative (see spec §API read path): the
     * {@code tenant_id} filter is NOT optional and MUST NOT be bypassed.
     *
     * @param tenantId       tenant scope, resolved by the caller from the
     *                       authenticated principal
     * @param taskId         the task whose log is being read
     * @param afterSequence  exclusive lower bound on the monotone
     *                       {@code sequence} column (pass {@code 0} for first
     *                       page)
     * @param limit          maximum rows to return; caller is responsible for
     *                       clamping to any per-endpoint cap
     */
    public List<ConversationEntryResponse> findByTask(
            String tenantId, UUID taskId, long afterSequence, int limit) {
        String sql = """
                SELECT sequence, kind, role, content_version, content, metadata, content_size, created_at
                  FROM task_conversation_log
                 WHERE tenant_id = ?
                   AND task_id = ?
                   AND sequence > ?
                 ORDER BY sequence
                 LIMIT ?
                """;

        RowMapper<ConversationEntryResponse> rowMapper = (rs, rowNum) -> new ConversationEntryResponse(
                rs.getLong("sequence"),
                rs.getString("kind"),
                rs.getString("role"),
                rs.getInt("content_version"),
                parseJsonNode(rs.getString("content")),
                parseJsonNode(rs.getString("metadata")),
                rs.getInt("content_size"),
                DateTimeUtil.toOffsetDateTime(rs.getTimestamp("created_at"))
        );

        return jdbcTemplate.query(sql, rowMapper, tenantId, taskId, afterSequence, limit);
    }

    /**
     * Parses a Postgres {@code jsonb} column value (returned as String by the
     * PG JDBC driver) into a {@link JsonNode}. Falls back to an empty object
     * node on parse failure so a malformed row never crashes the read path —
     * the warning log surfaces the issue for ops.
     */
    private JsonNode parseJsonNode(String raw) {
        if (raw == null || raw.isBlank()) {
            return JsonNodeFactory.instance.objectNode();
        }
        try {
            return objectMapper.readTree(raw);
        } catch (JsonProcessingException e) {
            log.warn("Failed to parse task_conversation_log JSON payload: {}", e.getMessage());
            return JsonNodeFactory.instance.objectNode();
        }
    }
}
