package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.model.response.TaskPlanResponse;
import com.persistentagent.api.repository.TaskRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.sql.Timestamp;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.when;

/**
 * Planning Primitive P3 — TaskPlanService unit tests.
 *
 * <p>Pure Mockito — no DB. The checkpoint payload shape mirrors the worker's
 * RuntimeState.plan channel: a list of {id, title, status} dicts nested
 * under channel_values.plan in the checkpoint_payload JSONB column.
 */
@ExtendWith(MockitoExtension.class)
class TaskPlanServiceTest {

    @Mock
    private TaskRepository taskRepository;

    private TaskPlanService service;

    private final String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

    @BeforeEach
    void setUp() {
        ObjectMapper objectMapper = new ObjectMapper();
        objectMapper.registerModule(new JavaTimeModule());
        service = new TaskPlanService(taskRepository, objectMapper);
    }

    // --- 404 when task does not exist ---

    @Test
    void getPlan_taskNotFound_throws404() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());
        when(taskRepository.findByIdAndTenant(taskId, tenantId)).thenReturn(Optional.empty());

        assertThrows(TaskNotFoundException.class, () -> service.getPlan(taskId));
    }

    // --- Empty plan when task exists but has no checkpoint ---

    @Test
    void getPlan_taskExistsNoCheckpoint_returnsEmptyPlan() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.empty());
        when(taskRepository.findByIdAndTenant(taskId, tenantId))
                .thenReturn(Optional.of(Map.of("task_id", taskId)));

        TaskPlanResponse response = service.getPlan(taskId);

        assertEquals(taskId, response.taskId());
        assertNotNull(response.plan());
        assertTrue(response.plan().isEmpty());
        assertNull(response.updatedAt());
    }

    // --- Empty plan when checkpoint exists but no plan channel ---

    @Test
    void getPlan_checkpointNoPlanChannel_returnsEmptyPlan() {
        UUID taskId = UUID.randomUUID();
        String payload = """
                {
                  "channel_values": {
                    "messages": []
                  }
                }
                """;
        Timestamp created = Timestamp.from(Instant.parse("2026-06-01T10:00:00Z"));
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(Map.of(
                "checkpoint_id", "ckpt_1",
                "checkpoint_payload", payload,
                "created_at", created)));

        TaskPlanResponse response = service.getPlan(taskId);

        assertEquals(taskId, response.taskId());
        assertTrue(response.plan().isEmpty());
        // updated_at is set from checkpoint even when plan channel is absent
        assertNotNull(response.updatedAt());
    }

    // --- Populated plan returned with items in order ---

    @Test
    void getPlan_withPopulatedPlanChannel_returnsMappedItems() {
        UUID taskId = UUID.randomUUID();
        String payload = """
                {
                  "channel_values": {
                    "plan": [
                      {"id": "step-1", "title": "Research the topic", "status": "completed"},
                      {"id": "step-2", "title": "Draft outline", "status": "in_progress"},
                      {"id": "step-3", "title": "Write full report", "status": "pending"}
                    ]
                  }
                }
                """;
        Timestamp created = Timestamp.from(Instant.parse("2026-06-01T12:00:00Z"));
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(Map.of(
                "checkpoint_id", "ckpt_2",
                "checkpoint_payload", payload,
                "created_at", created)));

        TaskPlanResponse response = service.getPlan(taskId);

        assertEquals(taskId, response.taskId());
        assertEquals(3, response.plan().size());

        TaskPlanResponse.PlanItem item0 = response.plan().get(0);
        assertEquals("step-1", item0.id());
        assertEquals("Research the topic", item0.title());
        assertEquals("completed", item0.status());

        TaskPlanResponse.PlanItem item1 = response.plan().get(1);
        assertEquals("step-2", item1.id());
        assertEquals("Draft outline", item1.title());
        assertEquals("in_progress", item1.status());

        TaskPlanResponse.PlanItem item2 = response.plan().get(2);
        assertEquals("step-3", item2.id());
        assertEquals("Write full report", item2.title());
        assertEquals("pending", item2.status());

        // updated_at reflects the checkpoint's created_at
        OffsetDateTime expected = OffsetDateTime.of(2026, 6, 1, 12, 0, 0, 0, ZoneOffset.UTC);
        assertEquals(expected, response.updatedAt());
    }

    // --- updated_at reflects checkpoint created_at ---

    @Test
    void getPlan_updatedAtReflectsCheckpointCreatedAt() {
        UUID taskId = UUID.randomUUID();
        String payload = "{\"channel_values\":{\"plan\":[]}}";
        Timestamp created = Timestamp.from(Instant.parse("2026-05-15T08:30:00Z"));
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(Map.of(
                "checkpoint_id", "ckpt_3",
                "checkpoint_payload", payload,
                "created_at", created)));

        TaskPlanResponse response = service.getPlan(taskId);

        OffsetDateTime expected = OffsetDateTime.of(2026, 5, 15, 8, 30, 0, 0, ZoneOffset.UTC);
        assertEquals(expected, response.updatedAt());
    }

    // --- Empty plan list (plan channel exists but is empty) ---

    @Test
    void getPlan_emptyPlanList_returns200WithEmptyPlan() {
        UUID taskId = UUID.randomUUID();
        String payload = "{\"channel_values\":{\"plan\":[]}}";
        Timestamp created = Timestamp.from(Instant.parse("2026-06-01T09:00:00Z"));
        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(Map.of(
                "checkpoint_id", "ckpt_4",
                "checkpoint_payload", payload,
                "created_at", created)));

        TaskPlanResponse response = service.getPlan(taskId);

        assertEquals(taskId, response.taskId());
        assertNotNull(response.plan());
        assertTrue(response.plan().isEmpty());
        assertNotNull(response.updatedAt());
    }

    // --- Payload provided as Map (not a JSON string) — mirrors ActivityProjectionService behavior ---

    @Test
    void getPlan_payloadAsMap_resolvesPlanItems() {
        UUID taskId = UUID.randomUUID();
        Map<String, Object> planItem = new HashMap<>();
        planItem.put("id", "task-a");
        planItem.put("title", "First step");
        planItem.put("status", "pending");

        Map<String, Object> payload = Map.of(
                "channel_values", Map.of("plan", List.of(planItem)));

        Timestamp created = Timestamp.from(Instant.parse("2026-06-02T00:00:00Z"));
        Map<String, Object> row = new HashMap<>();
        row.put("checkpoint_id", "ckpt_5");
        row.put("checkpoint_payload", payload);
        row.put("created_at", created);

        when(taskRepository.getLatestRootCheckpoint(taskId, tenantId)).thenReturn(Optional.of(row));

        TaskPlanResponse response = service.getPlan(taskId);

        assertEquals(1, response.plan().size());
        assertEquals("task-a", response.plan().get(0).id());
        assertEquals("First step", response.plan().get(0).title());
        assertEquals("pending", response.plan().get(0).status());
    }
}
