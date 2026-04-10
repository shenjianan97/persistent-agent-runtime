package com.persistentagent.api.controller;

import com.persistentagent.api.exception.ToolServerNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.response.DiscoveredToolInfo;
import com.persistentagent.api.model.response.ToolDiscoverResponse;
import com.persistentagent.api.model.response.ToolServerResponse;
import com.persistentagent.api.model.response.ToolServerSummaryResponse;
import com.persistentagent.api.service.ToolServerService;
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
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(ToolServerController.class)
class ToolServerControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private ToolServerService toolServerService;

    private static final String SERVER_ID = "550e8400-e29b-41d4-a716-446655440000";
    private static final String MISSING_ID = "00000000-0000-0000-0000-000000000000";
    private static final OffsetDateTime NOW = OffsetDateTime.now(ZoneOffset.UTC);

    private ToolServerResponse buildResponse(String name, String authToken) {
        return new ToolServerResponse(
            SERVER_ID, "default", name,
            "http://localhost:8080/mcp", "none", authToken,
            "active", NOW, NOW
        );
    }

    private ToolServerSummaryResponse buildSummaryResponse(String name) {
        return new ToolServerSummaryResponse(
            SERVER_ID, "default", name,
            "http://localhost:8080/mcp", "none",
            "active", NOW, NOW
        );
    }

    // --- POST /v1/tool-servers ---

    @Test
    void testCreateToolServer_success() throws Exception {
        ToolServerResponse response = buildResponse("my-server", null);
        when(toolServerService.createToolServer(any())).thenReturn(response);

        mockMvc.perform(post("/v1/tool-servers")
                .contentType(MediaType.APPLICATION_JSON)
                .content("""
                    {"name": "my-server", "url": "http://localhost:8080/mcp"}
                    """))
            .andExpect(status().isCreated())
            .andExpect(jsonPath("$.server_id").value(SERVER_ID))
            .andExpect(jsonPath("$.name").value("my-server"))
            .andExpect(jsonPath("$.status").value("active"));
    }

    @Test
    void testCreateToolServer_missingName() throws Exception {
        mockMvc.perform(post("/v1/tool-servers")
                .contentType(MediaType.APPLICATION_JSON)
                .content("""
                    {"url": "http://localhost:8080/mcp"}
                    """))
            .andExpect(status().isBadRequest());
    }

    @Test
    void testCreateToolServer_invalidNamePattern() throws Exception {
        mockMvc.perform(post("/v1/tool-servers")
                .contentType(MediaType.APPLICATION_JSON)
                .content("""
                    {"name": "My-Server", "url": "http://localhost:8080/mcp"}
                    """))
            .andExpect(status().isBadRequest());
    }

    @Test
    void testCreateToolServer_missingUrl() throws Exception {
        mockMvc.perform(post("/v1/tool-servers")
                .contentType(MediaType.APPLICATION_JSON)
                .content("""
                    {"name": "my-server"}
                    """))
            .andExpect(status().isBadRequest());
    }

    // --- GET /v1/tool-servers ---

    @Test
    void testListToolServers_noFilter() throws Exception {
        ToolServerSummaryResponse item = buildSummaryResponse("my-server");
        when(toolServerService.listToolServers(isNull(), isNull())).thenReturn(List.of(item));

        mockMvc.perform(get("/v1/tool-servers"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$[0].server_id").value(SERVER_ID))
            .andExpect(jsonPath("$[0].name").value("my-server"))
            .andExpect(jsonPath("$[0].status").value("active"));
    }

    @Test
    void testListToolServers_withStatusFilter() throws Exception {
        ToolServerSummaryResponse item = buildSummaryResponse("my-server");
        when(toolServerService.listToolServers(eq("active"), isNull())).thenReturn(List.of(item));

        mockMvc.perform(get("/v1/tool-servers").param("status", "active"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$[0].status").value("active"));
    }

    // --- GET /v1/tool-servers/{serverId} ---

    @Test
    void testGetToolServer_found() throws Exception {
        ToolServerResponse response = buildResponse("my-server", "ghp_...xxxx");
        when(toolServerService.getToolServer(SERVER_ID)).thenReturn(response);

        mockMvc.perform(get("/v1/tool-servers/" + SERVER_ID))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.server_id").value(SERVER_ID))
            .andExpect(jsonPath("$.name").value("my-server"))
            .andExpect(jsonPath("$.auth_token").value("ghp_...xxxx"));
    }

    @Test
    void testGetToolServer_notFound() throws Exception {
        when(toolServerService.getToolServer(MISSING_ID))
            .thenThrow(new ToolServerNotFoundException(MISSING_ID));

        mockMvc.perform(get("/v1/tool-servers/" + MISSING_ID))
            .andExpect(status().isNotFound())
            .andExpect(jsonPath("$.message").exists());
    }

    @Test
    void testGetToolServer_invalidUuid_returns400() throws Exception {
        mockMvc.perform(get("/v1/tool-servers/not-a-uuid"))
            .andExpect(status().isBadRequest())
            .andExpect(jsonPath("$.message").exists());
    }

    // --- PUT /v1/tool-servers/{serverId} ---

    @Test
    void testUpdateToolServer_success() throws Exception {
        ToolServerResponse response = buildResponse("updated-server", null);
        when(toolServerService.updateToolServer(eq(SERVER_ID), any())).thenReturn(response);

        mockMvc.perform(put("/v1/tool-servers/" + SERVER_ID)
                .contentType(MediaType.APPLICATION_JSON)
                .content("""
                    {"name": "updated-server"}
                    """))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.name").value("updated-server"));
    }

    @Test
    void testUpdateToolServer_notFound() throws Exception {
        when(toolServerService.updateToolServer(eq(MISSING_ID), any()))
            .thenThrow(new ToolServerNotFoundException(MISSING_ID));

        mockMvc.perform(put("/v1/tool-servers/" + MISSING_ID)
                .contentType(MediaType.APPLICATION_JSON)
                .content("""
                    {"name": "updated-server"}
                    """))
            .andExpect(status().isNotFound())
            .andExpect(jsonPath("$.message").exists());
    }

    // --- DELETE /v1/tool-servers/{serverId} ---

    @Test
    void testDeleteToolServer_success() throws Exception {
        doNothing().when(toolServerService).deleteToolServer(SERVER_ID);

        mockMvc.perform(delete("/v1/tool-servers/" + SERVER_ID))
            .andExpect(status().isNoContent());
    }

    @Test
    void testDeleteToolServer_notFound() throws Exception {
        doThrow(new ToolServerNotFoundException(MISSING_ID))
            .when(toolServerService).deleteToolServer(MISSING_ID);

        mockMvc.perform(delete("/v1/tool-servers/" + MISSING_ID))
            .andExpect(status().isNotFound())
            .andExpect(jsonPath("$.message").exists());
    }

    // --- POST /v1/tool-servers/{serverId}/discover ---

    @Test
    void testDiscoverTools_reachable() throws Exception {
        DiscoveredToolInfo tool = new DiscoveredToolInfo("my-tool", "A useful tool", Map.of("type", "object"));
        ToolDiscoverResponse response = new ToolDiscoverResponse(SERVER_ID, "my-server", "reachable", null, List.of(tool));
        when(toolServerService.discoverTools(SERVER_ID)).thenReturn(response);

        mockMvc.perform(post("/v1/tool-servers/" + SERVER_ID + "/discover"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.server_id").value(SERVER_ID))
            .andExpect(jsonPath("$.status").value("reachable"))
            .andExpect(jsonPath("$.tools[0].name").value("my-tool"));
    }

    @Test
    void testDiscoverTools_unreachable() throws Exception {
        ToolDiscoverResponse response = new ToolDiscoverResponse(SERVER_ID, "my-server", "unreachable", "Connection refused", List.of());
        when(toolServerService.discoverTools(SERVER_ID)).thenReturn(response);

        mockMvc.perform(post("/v1/tool-servers/" + SERVER_ID + "/discover"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.status").value("unreachable"))
            .andExpect(jsonPath("$.error").value("Connection refused"))
            .andExpect(jsonPath("$.tools").isEmpty());
    }

    @Test
    void testDiscoverTools_serverNotFound() throws Exception {
        when(toolServerService.discoverTools(MISSING_ID))
            .thenThrow(new ToolServerNotFoundException(MISSING_ID));

        mockMvc.perform(post("/v1/tool-servers/" + MISSING_ID + "/discover"))
            .andExpect(status().isNotFound())
            .andExpect(jsonPath("$.message").exists());
    }
}
