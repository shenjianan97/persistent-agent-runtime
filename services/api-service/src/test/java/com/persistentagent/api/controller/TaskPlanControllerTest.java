package com.persistentagent.api.controller;

import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.model.response.TaskPlanResponse;
import com.persistentagent.api.service.ActivityProjectionService;
import com.persistentagent.api.service.TaskEventService;
import com.persistentagent.api.service.TaskPlanService;
import com.persistentagent.api.service.TaskService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;

import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

/**
 * Planning Primitive P3 — TaskController plan endpoint tests.
 *
 * <p>Tests the three response shapes for {@code GET /v1/tasks/{taskId}/plan}:
 * <ol>
 *   <li>Populated plan → 200 with plan items</li>
 *   <li>Existing task, no plan / no checkpoint → 200 with empty plan</li>
 *   <li>Nonexistent task → 404</li>
 * </ol>
 */
@WebMvcTest(TaskController.class)
class TaskPlanControllerTest {

    @Autowired
    private MockMvc mockMvc;

    // Required beans for TaskController constructor
    @MockitoBean
    private TaskService taskService;

    @MockitoBean
    private TaskEventService taskEventService;

    @MockitoBean
    private ActivityProjectionService activityProjectionService;

    @MockitoBean
    private TaskPlanService taskPlanService;

    // --- 200: populated plan ---

    @Test
    void getPlan_populatedPlan_returns200WithItems() throws Exception {
        UUID taskId = UUID.randomUUID();
        OffsetDateTime updatedAt = OffsetDateTime.of(2026, 6, 1, 12, 0, 0, 0, ZoneOffset.UTC);
        List<TaskPlanResponse.PlanItem> items = List.of(
                new TaskPlanResponse.PlanItem("step-1", "Research the topic", "completed"),
                new TaskPlanResponse.PlanItem("step-2", "Write report", "in_progress")
        );
        TaskPlanResponse response = new TaskPlanResponse(taskId, items, updatedAt);
        when(taskPlanService.getPlan(taskId)).thenReturn(response);

        mockMvc.perform(get("/v1/tasks/{taskId}/plan", taskId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.task_id").value(taskId.toString()))
                .andExpect(jsonPath("$.plan").isArray())
                .andExpect(jsonPath("$.plan.length()").value(2))
                .andExpect(jsonPath("$.plan[0].id").value("step-1"))
                .andExpect(jsonPath("$.plan[0].title").value("Research the topic"))
                .andExpect(jsonPath("$.plan[0].status").value("completed"))
                .andExpect(jsonPath("$.plan[1].id").value("step-2"))
                .andExpect(jsonPath("$.plan[1].status").value("in_progress"))
                .andExpect(jsonPath("$.updated_at").exists());
    }

    // --- 200: existing task, empty plan ---

    @Test
    void getPlan_existingTaskNoPlan_returns200WithEmptyList() throws Exception {
        UUID taskId = UUID.randomUUID();
        TaskPlanResponse response = new TaskPlanResponse(taskId, List.of(), null);
        when(taskPlanService.getPlan(taskId)).thenReturn(response);

        mockMvc.perform(get("/v1/tasks/{taskId}/plan", taskId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.task_id").value(taskId.toString()))
                .andExpect(jsonPath("$.plan").isArray())
                .andExpect(jsonPath("$.plan.length()").value(0));
    }

    // --- 404: nonexistent task ---

    @Test
    void getPlan_taskNotFound_returns404() throws Exception {
        UUID taskId = UUID.randomUUID();
        when(taskPlanService.getPlan(taskId)).thenThrow(new TaskNotFoundException(taskId));

        mockMvc.perform(get("/v1/tasks/{taskId}/plan", taskId))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").exists());
    }
}
