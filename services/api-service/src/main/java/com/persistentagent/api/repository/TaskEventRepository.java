package com.persistentagent.api.repository;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.model.response.TaskEventResponse;
import com.persistentagent.api.util.DateTimeUtil;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Repository
public class TaskEventRepository {

    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;

    public TaskEventRepository(JdbcTemplate jdbcTemplate, ObjectMapper objectMapper) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
    }

    /**
     * Inserts a new task event. Failures propagate so the caller can roll back
     * the paired task-state transition.
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
        jdbcTemplate.update(sql,
                tenantId, taskId, agentId, eventType,
                statusBefore, statusAfter, workerId,
                errorCode, errorMessage, detailsJson != null ? detailsJson : "{}");
    }

    /**
     * Lists events for a task in chronological order (created_at ASC).
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
        return jdbcTemplate.query(sql, new TaskEventRowMapper(), taskId, tenantId, limit);
    }

    private class TaskEventRowMapper implements RowMapper<TaskEventResponse> {
        @Override
        public TaskEventResponse mapRow(ResultSet rs, int rowNum) throws SQLException {
            Object details = null;
            String detailsStr = rs.getString("details");
            if (detailsStr != null) {
                try {
                    details = objectMapper.readValue(detailsStr, Map.class);
                } catch (Exception e) {
                    details = detailsStr;
                }
            }
            return new TaskEventResponse(
                    rs.getObject("event_id", UUID.class),
                    rs.getObject("task_id", UUID.class),
                    rs.getString("agent_id"),
                    rs.getString("event_type"),
                    rs.getString("status_before"),
                    rs.getString("status_after"),
                    rs.getString("worker_id"),
                    rs.getString("error_code"),
                    rs.getString("error_message"),
                    details,
                    DateTimeUtil.toOffsetDateTime(rs.getObject("created_at"))
            );
        }
    }
}
