package com.persistentagent.api.repository;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.model.response.TaskEventResponse;
import com.persistentagent.api.util.DateTimeUtil;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Map;
import java.util.UUID;

@Repository
public class TaskEventRepository {

    private static final Logger log = LoggerFactory.getLogger(TaskEventRepository.class);

    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;

    public TaskEventRepository(JdbcTemplate jdbcTemplate, ObjectMapper objectMapper) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
    }

    /**
     * Inserts a new task event. Failures propagate to the caller so that
     * the surrounding state-transition mutation can be rolled back.
     */
    public void insertEvent(String tenantId, UUID taskId, String agentId, String eventType,
                            String statusBefore, String statusAfter, String workerId,
                            String errorCode, String errorMessage, String detailsJson) {
        String sql = """
                INSERT INTO task_events (tenant_id, task_id, agent_id, event_type,
                                         status_before, status_after, worker_id,
                                         error_code, error_message, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb)
                """;
        String details = detailsJson != null ? detailsJson : "{}";
        jdbcTemplate.update(sql, tenantId, taskId, agentId, eventType,
                statusBefore, statusAfter, workerId, errorCode, errorMessage, details);
    }

    /**
     * Lists events for a task in chronological order (oldest first), limited to the given count.
     */
    public List<TaskEventResponse> listEvents(UUID taskId, String tenantId, int limit) {
        String sql = """
                SELECT event_id, task_id, agent_id, event_type,
                       status_before, status_after, worker_id,
                       error_code, error_message, details, created_at
                FROM task_events
                WHERE task_id = ? AND tenant_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """;

        RowMapper<TaskEventResponse> rowMapper = (rs, rowNum) -> {
            Object details = parseDetailsJson(rs.getString("details"));
            return new TaskEventResponse(
                    (UUID) rs.getObject("event_id"),
                    (UUID) rs.getObject("task_id"),
                    rs.getString("agent_id"),
                    rs.getString("event_type"),
                    rs.getString("status_before"),
                    rs.getString("status_after"),
                    rs.getString("worker_id"),
                    rs.getString("error_code"),
                    rs.getString("error_message"),
                    details,
                    DateTimeUtil.toOffsetDateTime(rs.getTimestamp("created_at"))
            );
        };

        return jdbcTemplate.query(sql, rowMapper, taskId, tenantId, limit);
    }

    @SuppressWarnings("unchecked")
    private Object parseDetailsJson(String json) {
        if (json == null || json.isBlank() || "{}".equals(json)) {
            return Map.of();
        }
        try {
            return objectMapper.readValue(json, Map.class);
        } catch (Exception e) {
            log.warn("Failed to parse task_events details JSON: {}", e.getMessage());
            return Map.of();
        }
    }
}
