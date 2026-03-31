package com.persistentagent.api.controller;

import com.persistentagent.api.model.response.LangfuseEndpointResponse;
import com.persistentagent.api.model.response.LangfuseEndpointTestResponse;
import com.persistentagent.api.service.LangfuseEndpointService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.doNothing;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(LangfuseEndpointController.class)
class LangfuseEndpointControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private LangfuseEndpointService langfuseEndpointService;

    private static final String VALID_BODY = """
            {
              "name": "my-langfuse",
              "host": "https://langfuse.example.com",
              "public_key": "pk-test-123",
              "secret_key": "sk-test-456"
            }
            """;

    // --- POST /v1/langfuse-endpoints ---

    @Test
    void createEndpoint_returns201() throws Exception {
        UUID endpointId = UUID.randomUUID();
        LangfuseEndpointResponse response = new LangfuseEndpointResponse(
                endpointId, "default", "my-langfuse", "https://langfuse.example.com",
                Instant.now(), null);
        when(langfuseEndpointService.create(anyString(), any())).thenReturn(response);

        mockMvc.perform(post("/v1/langfuse-endpoints")
                        .contentType(MediaType.APPLICATION_JSON_VALUE)
                        .content(VALID_BODY))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.endpoint_id").value(endpointId.toString()))
                .andExpect(jsonPath("$.name").value("my-langfuse"))
                .andExpect(jsonPath("$.host").value("https://langfuse.example.com"));
    }

    // --- GET /v1/langfuse-endpoints ---

    @Test
    void listEndpoints_returns200() throws Exception {
        UUID endpointId = UUID.randomUUID();
        LangfuseEndpointResponse item = new LangfuseEndpointResponse(
                endpointId, "default", "my-langfuse", "https://langfuse.example.com",
                Instant.now(), null);
        when(langfuseEndpointService.list(anyString())).thenReturn(List.of(item));

        mockMvc.perform(get("/v1/langfuse-endpoints"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].endpoint_id").value(endpointId.toString()))
                .andExpect(jsonPath("$[0].name").value("my-langfuse"));
    }

    // --- GET /v1/langfuse-endpoints/{id} ---

    @Test
    void getEndpoint_returns200() throws Exception {
        UUID endpointId = UUID.randomUUID();
        LangfuseEndpointResponse response = new LangfuseEndpointResponse(
                endpointId, "default", "my-langfuse", "https://langfuse.example.com",
                Instant.now(), Instant.now());
        when(langfuseEndpointService.get(eq(endpointId), anyString())).thenReturn(response);

        mockMvc.perform(get("/v1/langfuse-endpoints/" + endpointId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.endpoint_id").value(endpointId.toString()))
                .andExpect(jsonPath("$.name").value("my-langfuse"));
    }

    @Test
    void getEndpoint_notFound_returns404() throws Exception {
        UUID endpointId = UUID.randomUUID();
        when(langfuseEndpointService.get(eq(endpointId), anyString()))
                .thenThrow(new LangfuseEndpointService.NotFoundException("Langfuse endpoint not found: " + endpointId));

        mockMvc.perform(get("/v1/langfuse-endpoints/" + endpointId))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").exists());
    }

    // --- PUT /v1/langfuse-endpoints/{id} ---

    @Test
    void updateEndpoint_returns200() throws Exception {
        UUID endpointId = UUID.randomUUID();
        LangfuseEndpointResponse response = new LangfuseEndpointResponse(
                endpointId, "default", "my-langfuse", "https://langfuse.example.com",
                Instant.now(), Instant.now());
        when(langfuseEndpointService.update(eq(endpointId), anyString(), any())).thenReturn(response);

        mockMvc.perform(put("/v1/langfuse-endpoints/" + endpointId)
                        .contentType(MediaType.APPLICATION_JSON_VALUE)
                        .content(VALID_BODY))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.endpoint_id").value(endpointId.toString()))
                .andExpect(jsonPath("$.name").value("my-langfuse"));
    }

    // --- DELETE /v1/langfuse-endpoints/{id} ---

    @Test
    void deleteEndpoint_returns204() throws Exception {
        UUID endpointId = UUID.randomUUID();
        doNothing().when(langfuseEndpointService).delete(eq(endpointId), anyString());

        mockMvc.perform(delete("/v1/langfuse-endpoints/" + endpointId))
                .andExpect(status().isNoContent());
    }

    @Test
    void deleteEndpoint_conflict_returns409() throws Exception {
        UUID endpointId = UUID.randomUUID();
        doThrow(new LangfuseEndpointService.ConflictException(
                "Langfuse endpoint is referenced by active tasks"))
                .when(langfuseEndpointService).delete(eq(endpointId), anyString());

        mockMvc.perform(delete("/v1/langfuse-endpoints/" + endpointId))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.message").exists());
    }

    // --- POST /v1/langfuse-endpoints/{id}/test ---

    @Test
    void testConnectivity_returns200() throws Exception {
        UUID endpointId = UUID.randomUUID();
        LangfuseEndpointTestResponse response = new LangfuseEndpointTestResponse(true, "OK");
        when(langfuseEndpointService.testConnectivity(eq(endpointId), anyString())).thenReturn(response);

        mockMvc.perform(post("/v1/langfuse-endpoints/" + endpointId + "/test"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.reachable").value(true))
                .andExpect(jsonPath("$.message").value("OK"));
    }
}
