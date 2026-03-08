package com.persistentagent.api.controller;

import com.persistentagent.api.model.response.HealthResponse;
import com.persistentagent.api.service.TaskService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(HealthController.class)
class HealthControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private TaskService taskService;

    @Test
    void health_dbConnected_returnsHealthy() throws Exception {
        when(taskService.getHealth())
                .thenReturn(new HealthResponse("healthy", "connected", 3, 12));

        mockMvc.perform(get("/v1/health"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("healthy"))
                .andExpect(jsonPath("$.database").value("connected"))
                .andExpect(jsonPath("$.active_workers").value(3))
                .andExpect(jsonPath("$.queued_tasks").value(12));
    }

    @Test
    void health_dbDisconnected_returns503() throws Exception {
        when(taskService.getHealth())
                .thenReturn(new HealthResponse("unhealthy", "disconnected", 0, 0));

        mockMvc.perform(get("/v1/health"))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.status").value("unhealthy"));
    }
}
