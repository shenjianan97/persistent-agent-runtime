package com.persistentagent.api.controller;

import com.persistentagent.api.exception.AgentNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.response.AgentResponse;
import com.persistentagent.api.model.response.AgentSummaryResponse;
import com.persistentagent.api.service.AgentService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(AgentController.class)
class AgentControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private AgentService agentService;

    private static final String VALID_CREATE_BODY = """
            {
              "display_name": "Test Agent",
              "agent_config": {
                "system_prompt": "You are a helpful assistant.",
                "provider": "openai",
                "model": "gpt-4o",
                "temperature": 0.7,
                "allowed_tools": ["web_search"]
              }
            }
            """;

    private static final String VALID_UPDATE_BODY = """
            {
              "display_name": "Updated Agent",
              "agent_config": {
                "system_prompt": "You are an updated assistant.",
                "provider": "openai",
                "model": "gpt-4o",
                "temperature": 0.5,
                "allowed_tools": []
              },
              "status": "active"
            }
            """;

    // --- POST /v1/agents ---

    @Test
    void createAgent_returns201() throws Exception {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        Object configObj = Map.of("system_prompt", "You are a helpful assistant.",
                "provider", "openai", "model", "gpt-4o",
                "temperature", 0.7, "allowed_tools", List.of("web_search"));
        AgentResponse response = new AgentResponse(
                "generated-uuid", "Test Agent", configObj, "active", now, now);
        when(agentService.createAgent(any())).thenReturn(response);

        mockMvc.perform(post("/v1/agents")
                        .contentType(MediaType.APPLICATION_JSON_VALUE)
                        .content(VALID_CREATE_BODY))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.agent_id").value("generated-uuid"))
                .andExpect(jsonPath("$.display_name").value("Test Agent"))
                .andExpect(jsonPath("$.status").value("active"));
    }

    @Test
    void createAgent_missingDisplayName_returns400() throws Exception {
        String body = """
                {
                  "agent_config": {
                    "system_prompt": "prompt",
                    "provider": "openai",
                    "model": "gpt-4o"
                  }
                }
                """;

        mockMvc.perform(post("/v1/agents")
                        .contentType(MediaType.APPLICATION_JSON_VALUE)
                        .content(body))
                .andExpect(status().isBadRequest());
    }

    @Test
    void createAgent_missingAgentConfig_returns400() throws Exception {
        String body = """
                {
                  "display_name": "Test Agent"
                }
                """;

        mockMvc.perform(post("/v1/agents")
                        .contentType(MediaType.APPLICATION_JSON_VALUE)
                        .content(body))
                .andExpect(status().isBadRequest());
    }

    // --- GET /v1/agents ---

    @Test
    void listAgents_returns200() throws Exception {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        AgentSummaryResponse item = new AgentSummaryResponse(
                "test-agent", "Test Agent", "openai", "gpt-4o", "active", now, now);
        when(agentService.listAgents(isNull(), isNull())).thenReturn(List.of(item));

        mockMvc.perform(get("/v1/agents"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].agent_id").value("test-agent"))
                .andExpect(jsonPath("$[0].display_name").value("Test Agent"))
                .andExpect(jsonPath("$[0].provider").value("openai"))
                .andExpect(jsonPath("$[0].model").value("gpt-4o"))
                .andExpect(jsonPath("$[0].status").value("active"));
    }

    @Test
    void listAgents_withStatusFilter_returns200() throws Exception {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        AgentSummaryResponse item = new AgentSummaryResponse(
                "test-agent", "Test Agent", "openai", "gpt-4o", "disabled", now, now);
        when(agentService.listAgents(eq("disabled"), isNull())).thenReturn(List.of(item));

        mockMvc.perform(get("/v1/agents").param("status", "disabled"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].status").value("disabled"));
    }

    @Test
    void listAgents_invalidStatus_returns400() throws Exception {
        when(agentService.listAgents(eq("garbage"), isNull()))
                .thenThrow(new ValidationException("Invalid status filter: garbage"));

        mockMvc.perform(get("/v1/agents").param("status", "garbage"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").exists());
    }

    // --- GET /v1/agents/{agentId} ---

    @Test
    void getAgent_returns200() throws Exception {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        Object configObj = Map.of("system_prompt", "prompt", "provider", "openai", "model", "gpt-4o");
        AgentResponse response = new AgentResponse(
                "test-agent", "Test Agent", configObj, "active", now, now);
        when(agentService.getAgent("test-agent")).thenReturn(response);

        mockMvc.perform(get("/v1/agents/test-agent"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.agent_id").value("test-agent"))
                .andExpect(jsonPath("$.display_name").value("Test Agent"))
                .andExpect(jsonPath("$.status").value("active"));
    }

    @Test
    void getAgent_notFound_returns404() throws Exception {
        when(agentService.getAgent("nonexistent"))
                .thenThrow(new AgentNotFoundException("nonexistent"));

        mockMvc.perform(get("/v1/agents/nonexistent"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").exists());
    }

    // --- PUT /v1/agents/{agentId} ---

    @Test
    void updateAgent_returns200() throws Exception {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        Object configObj = Map.of("system_prompt", "You are an updated assistant.",
                "provider", "openai", "model", "gpt-4o");
        AgentResponse response = new AgentResponse(
                "test-agent", "Updated Agent", configObj, "active", now, now);
        when(agentService.updateAgent(eq("test-agent"), any())).thenReturn(response);

        mockMvc.perform(put("/v1/agents/test-agent")
                        .contentType(MediaType.APPLICATION_JSON_VALUE)
                        .content(VALID_UPDATE_BODY))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.agent_id").value("test-agent"))
                .andExpect(jsonPath("$.display_name").value("Updated Agent"));
    }

    @Test
    void updateAgent_notFound_returns404() throws Exception {
        when(agentService.updateAgent(eq("nonexistent"), any()))
                .thenThrow(new AgentNotFoundException("nonexistent"));

        mockMvc.perform(put("/v1/agents/nonexistent")
                        .contentType(MediaType.APPLICATION_JSON_VALUE)
                        .content(VALID_UPDATE_BODY))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").exists());
    }

    @Test
    void updateAgent_missingDisplayName_returns400() throws Exception {
        String body = """
                {
                  "agent_config": {
                    "system_prompt": "prompt",
                    "provider": "openai",
                    "model": "gpt-4o"
                  },
                  "status": "active"
                }
                """;

        mockMvc.perform(put("/v1/agents/test-agent")
                        .contentType(MediaType.APPLICATION_JSON_VALUE)
                        .content(body))
                .andExpect(status().isBadRequest());
    }

    @Test
    void updateAgent_missingStatus_returns400() throws Exception {
        String body = """
                {
                  "display_name": "Test Agent",
                  "agent_config": {
                    "system_prompt": "prompt",
                    "provider": "openai",
                    "model": "gpt-4o"
                  }
                }
                """;

        mockMvc.perform(put("/v1/agents/test-agent")
                        .contentType(MediaType.APPLICATION_JSON_VALUE)
                        .content(body))
                .andExpect(status().isBadRequest());
    }
}
