package com.persistentagent.api.repository;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.jdbc.core.JdbcTemplate;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class TaskRepositoryTest {

    @Mock
    private JdbcTemplate jdbcTemplate;

    private TaskRepository taskRepository;

    @BeforeEach
    void setUp() {
        taskRepository = new TaskRepository(jdbcTemplate);
    }

    @Test
    void findByIdWithAggregates_queryDoesNotSelectLegacyCheckpointCostTotals() {
        UUID taskId = UUID.randomUUID();
        Timestamp now = Timestamp.from(Instant.now());
        when(jdbcTemplate.queryForList(anyString(), eq(taskId), eq("default")))
                .thenReturn(List.of(taskRow(taskId, now)));

        Optional<Map<String, Object>> result = taskRepository.findByIdWithAggregates(taskId, "default");

        assertTrue(result.isPresent());
        ArgumentCaptor<String> sqlCaptor = ArgumentCaptor.forClass(String.class);
        verify(jdbcTemplate).queryForList(sqlCaptor.capture(), eq(taskId), eq("default"));
        String sql = sqlCaptor.getValue();
        assertTrue(sql.contains("checkpoint_count"));
        assertFalse(sql.contains("SUM(cost_microdollars)"));
        assertFalse(sql.contains("AS total_cost_microdollars"));
    }

    @Test
    void listTasks_queryDoesNotSelectLegacyCheckpointCostTotals() {
        Timestamp now = Timestamp.from(Instant.now());
        when(jdbcTemplate.queryForList(anyString(), any(Object[].class)))
                .thenReturn(List.of(Map.ofEntries(
                        Map.entry("task_id", UUID.randomUUID()),
                        Map.entry("agent_id", "agent-1"),
                        Map.entry("status", "completed"),
                        Map.entry("retry_count", 0),
                        Map.entry("checkpoint_count", 1L),
                        Map.entry("created_at", now),
                        Map.entry("updated_at", now)
                )));

        List<Map<String, Object>> result = taskRepository.listTasks("default", null, null, 50);

        assertEquals(1, result.size());
        ArgumentCaptor<String> sqlCaptor = ArgumentCaptor.forClass(String.class);
        verify(jdbcTemplate).queryForList(sqlCaptor.capture(), any(Object[].class));
        String sql = sqlCaptor.getValue();
        assertTrue(sql.contains("checkpoint_count"));
        assertFalse(sql.contains("SUM(c.cost_microdollars)"));
        assertFalse(sql.contains("AS total_cost_microdollars"));
    }

    private Map<String, Object> taskRow(UUID taskId, Timestamp now) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("task_id", taskId);
        row.put("tenant_id", "default");
        row.put("agent_id", "agent-1");
        row.put("status", "completed");
        row.put("input", "test");
        row.put("output", null);
        row.put("retry_count", 0);
        row.put("retry_history", "[]");
        row.put("lease_owner", null);
        row.put("last_error_code", null);
        row.put("last_error_message", null);
        row.put("last_worker_id", null);
        row.put("dead_letter_reason", null);
        row.put("dead_lettered_at", null);
        row.put("created_at", now);
        row.put("updated_at", now);
        row.put("checkpoint_count", 2L);
        return row;
    }
}
