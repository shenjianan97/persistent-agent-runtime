package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.exception.ToolServerNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.ToolServerCreateRequest;
import com.persistentagent.api.model.request.ToolServerUpdateRequest;
import com.persistentagent.api.model.response.ToolDiscoverResponse;
import com.persistentagent.api.model.response.ToolServerResponse;
import com.persistentagent.api.model.response.ToolServerSummaryResponse;
import com.persistentagent.api.repository.ToolServerRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.dao.DuplicateKeyException;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class ToolServerServiceTest {

    @Mock
    private ToolServerRepository repository;

    private final ObjectMapper objectMapper = new ObjectMapper();

    private ToolServerService service;

    private static final String TENANT_ID = "default";
    private static final String SERVER_ID = "550e8400-e29b-41d4-a716-446655440000";

    @BeforeEach
    void setUp() {
        service = new ToolServerService(repository, objectMapper);
    }

    // --- Helper ---

    private Map<String, Object> buildRow(String serverId, String name, String url,
                                          String authType, String authToken, String status) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("server_id", UUID.fromString(serverId));
        row.put("tenant_id", TENANT_ID);
        row.put("name", name);
        row.put("url", url);
        row.put("auth_type", authType);
        row.put("auth_token", authToken);
        row.put("status", status);
        row.put("created_at", Timestamp.from(Instant.now()));
        row.put("updated_at", Timestamp.from(Instant.now()));
        return row;
    }

    private Map<String, Object> buildSummaryRow(String serverId, String name, String url,
                                                  String authType, String status) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("server_id", UUID.fromString(serverId));
        row.put("tenant_id", TENANT_ID);
        row.put("name", name);
        row.put("url", url);
        row.put("auth_type", authType);
        row.put("status", status);
        row.put("created_at", Timestamp.from(Instant.now()));
        row.put("updated_at", Timestamp.from(Instant.now()));
        return row;
    }

    // --- createToolServer tests ---

    @Test
    void testCreateToolServer_success() {
        ToolServerCreateRequest request = new ToolServerCreateRequest(
            "my-server", "http://localhost:8080/mcp", "none", null
        );
        Map<String, Object> row = buildRow(SERVER_ID, "my-server", "http://localhost:8080/mcp", "none", null, "active");
        when(repository.insert(eq(TENANT_ID), eq("my-server"), eq("http://localhost:8080/mcp"), eq("none"), isNull()))
            .thenReturn(row);

        ToolServerResponse response = service.createToolServer(request);

        assertNotNull(response);
        assertEquals(SERVER_ID, response.serverId());
        assertEquals("my-server", response.name());
        assertEquals("active", response.status());
        assertNull(response.authToken()); // no token for auth_type=none
    }

    @Test
    void testCreateToolServer_withBearerToken_success() {
        ToolServerCreateRequest request = new ToolServerCreateRequest(
            "my-server", "http://localhost:8080/mcp", "bearer_token", "supersecrettoken123456"
        );
        Map<String, Object> row = buildRow(SERVER_ID, "my-server", "http://localhost:8080/mcp", "bearer_token", "supersecrettoken123456", "active");
        when(repository.insert(eq(TENANT_ID), eq("my-server"), eq("http://localhost:8080/mcp"), eq("bearer_token"), eq("supersecrettoken123456")))
            .thenReturn(row);

        ToolServerResponse response = service.createToolServer(request);

        assertNotNull(response);
        // Token should be masked
        assertNotNull(response.authToken());
        assertTrue(response.authToken().contains("..."));
    }

    @Test
    void testCreateToolServer_duplicateName_throwsValidation() {
        ToolServerCreateRequest request = new ToolServerCreateRequest(
            "my-server", "http://localhost:8080/mcp", "none", null
        );
        when(repository.insert(any(), any(), any(), any(), any()))
            .thenThrow(new DuplicateKeyException("duplicate key"));

        assertThrows(ValidationException.class, () -> service.createToolServer(request));
    }

    @Test
    void testCreateToolServer_bearerWithoutToken_throwsValidation() {
        ToolServerCreateRequest request = new ToolServerCreateRequest(
            "my-server", "http://localhost:8080/mcp", "bearer_token", null
        );

        assertThrows(ValidationException.class, () -> service.createToolServer(request));
    }

    @Test
    void testCreateToolServer_bearerWithBlankToken_throwsValidation() {
        ToolServerCreateRequest request = new ToolServerCreateRequest(
            "my-server", "http://localhost:8080/mcp", "bearer_token", "   "
        );

        assertThrows(ValidationException.class, () -> service.createToolServer(request));
    }

    @Test
    void testCreateToolServer_invalidUrl_throwsValidation() {
        ToolServerCreateRequest request = new ToolServerCreateRequest(
            "my-server", "ftp://invalid-scheme.example.com", "none", null
        );

        assertThrows(ValidationException.class, () -> service.createToolServer(request));
    }

    @Test
    void testCreateToolServer_invalidAuthType_throwsValidation() {
        ToolServerCreateRequest request = new ToolServerCreateRequest(
            "my-server", "http://localhost:8080/mcp", "api_key", "sometoken"
        );

        assertThrows(ValidationException.class, () -> service.createToolServer(request));
    }

    // --- listToolServers tests ---

    @Test
    void testListToolServers_noFilter() {
        Map<String, Object> row = buildSummaryRow(SERVER_ID, "my-server", "http://localhost:8080/mcp", "none", "active");
        when(repository.listByTenant(TENANT_ID, null, 50)).thenReturn(List.of(row));

        List<ToolServerSummaryResponse> result = service.listToolServers(null, null);

        assertEquals(1, result.size());
        assertEquals("my-server", result.get(0).name());
        assertEquals("active", result.get(0).status());
    }

    @Test
    void testListToolServers_withStatusFilter() {
        when(repository.listByTenant(TENANT_ID, "active", 50)).thenReturn(List.of());

        List<ToolServerSummaryResponse> result = service.listToolServers("active", null);

        assertEquals(0, result.size());
        verify(repository).listByTenant(TENANT_ID, "active", 50);
    }

    @Test
    void testListToolServers_invalidStatus_throwsValidation() {
        assertThrows(ValidationException.class,
            () -> service.listToolServers("invalid_status", null));
    }

    @Test
    void testListToolServers_limitCapped() {
        when(repository.listByTenant(TENANT_ID, null, 200)).thenReturn(List.of());

        service.listToolServers(null, 999);

        verify(repository).listByTenant(TENANT_ID, null, 200);
    }

    @Test
    void testListToolServers_limitFloorAt1() {
        when(repository.listByTenant(TENANT_ID, null, 1)).thenReturn(List.of());

        service.listToolServers(null, -5);

        verify(repository).listByTenant(TENANT_ID, null, 1);
    }

    // --- getToolServer tests ---

    @Test
    void testGetToolServer_found() {
        Map<String, Object> row = buildRow(SERVER_ID, "my-server", "http://localhost:8080/mcp", "none", null, "active");
        when(repository.findById(TENANT_ID, SERVER_ID)).thenReturn(Optional.of(row));

        ToolServerResponse response = service.getToolServer(SERVER_ID);

        assertEquals(SERVER_ID, response.serverId());
        assertEquals("my-server", response.name());
    }

    @Test
    void testGetToolServer_notFound_throwsException() {
        when(repository.findById(TENANT_ID, "nonexistent")).thenReturn(Optional.empty());

        assertThrows(ToolServerNotFoundException.class, () -> service.getToolServer("nonexistent"));
    }

    // --- updateToolServer tests ---

    @Test
    void testUpdateToolServer_success() {
        ToolServerUpdateRequest request = new ToolServerUpdateRequest(
            "updated-server", null, null, null, null
        );
        Map<String, Object> row = buildRow(SERVER_ID, "updated-server", "http://localhost:8080/mcp", "none", null, "active");
        when(repository.update(eq(TENANT_ID), eq(SERVER_ID), eq("updated-server"), isNull(), isNull(), isNull(), eq(false), isNull()))
            .thenReturn(Optional.of(row));

        ToolServerResponse response = service.updateToolServer(SERVER_ID, request);

        assertEquals("updated-server", response.name());
    }

    @Test
    void testUpdateToolServer_notFound_throwsException() {
        ToolServerUpdateRequest request = new ToolServerUpdateRequest(
            "updated-server", null, null, null, null
        );
        when(repository.update(any(), any(), any(), any(), any(), any(), anyBoolean(), any()))
            .thenReturn(Optional.empty());

        assertThrows(ToolServerNotFoundException.class,
            () -> service.updateToolServer("nonexistent", request));
    }

    @Test
    void testUpdateToolServer_invalidStatus_throwsValidation() {
        ToolServerUpdateRequest request = new ToolServerUpdateRequest(
            null, null, null, null, "invalid_status"
        );

        assertThrows(ValidationException.class,
            () -> service.updateToolServer(SERVER_ID, request));
    }

    @Test
    void testUpdateToolServer_invalidAuthType_throwsValidation() {
        ToolServerUpdateRequest request = new ToolServerUpdateRequest(
            null, null, "api_key", null, null
        );

        assertThrows(ValidationException.class,
            () -> service.updateToolServer(SERVER_ID, request));
    }

    @Test
    void testUpdateToolServer_duplicateName_throwsValidation() {
        ToolServerUpdateRequest request = new ToolServerUpdateRequest(
            "existing-server", null, null, null, null
        );
        when(repository.update(any(), any(), any(), any(), any(), any(), anyBoolean(), any()))
            .thenThrow(new DuplicateKeyException("duplicate key"));

        assertThrows(ValidationException.class,
            () -> service.updateToolServer(SERVER_ID, request));
    }

    @Test
    void testUpdateToolServer_switchToNone_clearAuthToken() {
        ToolServerUpdateRequest request = new ToolServerUpdateRequest(
            null, null, "none", null, null
        );
        Map<String, Object> row = buildRow(SERVER_ID, "my-server", "http://localhost:8080/mcp", "none", null, "active");
        when(repository.update(eq(TENANT_ID), eq(SERVER_ID), isNull(), isNull(), eq("none"), isNull(), eq(true), isNull()))
            .thenReturn(Optional.of(row));

        ToolServerResponse response = service.updateToolServer(SERVER_ID, request);

        assertNotNull(response);
        assertNull(response.authToken());
        verify(repository).update(eq(TENANT_ID), eq(SERVER_ID), isNull(), isNull(), eq("none"), isNull(), eq(true), isNull());
    }

    // --- deleteToolServer tests ---

    @Test
    void testDeleteToolServer_success() {
        when(repository.delete(TENANT_ID, SERVER_ID)).thenReturn(true);

        assertDoesNotThrow(() -> service.deleteToolServer(SERVER_ID));
    }

    @Test
    void testDeleteToolServer_notFound_throwsException() {
        when(repository.delete(TENANT_ID, "nonexistent")).thenReturn(false);

        assertThrows(ToolServerNotFoundException.class,
            () -> service.deleteToolServer("nonexistent"));
    }

    // --- Token masking tests ---

    @Test
    void testMaskToken_short() {
        ToolServerCreateRequest request = new ToolServerCreateRequest(
            "my-server", "http://localhost:8080/mcp", "bearer_token", "short"
        );
        Map<String, Object> row = buildRow(SERVER_ID, "my-server", "http://localhost:8080/mcp", "bearer_token", "short", "active");
        when(repository.insert(any(), any(), any(), any(), any())).thenReturn(row);

        ToolServerResponse response = service.createToolServer(request);

        // Token "short" is <= 8 chars, should be masked as "****"
        assertEquals("****", response.authToken());
    }

    @Test
    void testMaskToken_long() {
        ToolServerCreateRequest request = new ToolServerCreateRequest(
            "my-server", "http://localhost:8080/mcp", "bearer_token", "supersecrettoken123456"
        );
        Map<String, Object> row = buildRow(SERVER_ID, "my-server", "http://localhost:8080/mcp", "bearer_token", "supersecrettoken123456", "active");
        when(repository.insert(any(), any(), any(), any(), any())).thenReturn(row);

        ToolServerResponse response = service.createToolServer(request);

        // Token "supersecrettoken123456" is > 8 chars, should be "supe...3456"
        assertNotNull(response.authToken());
        assertTrue(response.authToken().startsWith("supe"));
        assertTrue(response.authToken().endsWith("3456"));
        assertTrue(response.authToken().contains("..."));
    }

    @Test
    void testMaskToken_null() {
        ToolServerCreateRequest request = new ToolServerCreateRequest(
            "my-server", "http://localhost:8080/mcp", "none", null
        );
        Map<String, Object> row = buildRow(SERVER_ID, "my-server", "http://localhost:8080/mcp", "none", null, "active");
        when(repository.insert(any(), any(), any(), any(), isNull())).thenReturn(row);

        ToolServerResponse response = service.createToolServer(request);

        assertNull(response.authToken());
    }

    // --- discoverTools tests ---

    @SuppressWarnings("unchecked")
    @Test
    void testDiscoverTools_reachableServer_returnsTools() throws Exception {
        Map<String, Object> row = buildRow(SERVER_ID, "my-server", "http://localhost:8080/mcp", "none", null, "active");
        when(repository.findById(TENANT_ID, SERVER_ID)).thenReturn(Optional.of(row));

        HttpClient mockHttpClient = mock(HttpClient.class);
        HttpResponse<String> initResponse = mock(HttpResponse.class);
        when(initResponse.statusCode()).thenReturn(200);
        when(initResponse.headers()).thenReturn(java.net.http.HttpHeaders.of(Map.of(), (a, b) -> true));

        HttpResponse<String> listResponse = mock(HttpResponse.class);
        when(listResponse.statusCode()).thenReturn(200);
        when(listResponse.body()).thenReturn(
            "{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"tools\":[{\"name\":\"echo\",\"description\":\"Echoes input\"}]}}"
        );

        when(mockHttpClient.send(any(HttpRequest.class), any(HttpResponse.BodyHandler.class)))
            .thenReturn(initResponse, listResponse);

        ToolServerService serviceWithMockHttp = new ToolServerService(repository, objectMapper, mockHttpClient);
        ToolDiscoverResponse response = serviceWithMockHttp.discoverTools(SERVER_ID);

        assertEquals("reachable", response.status());
        assertNull(response.error());
        assertEquals(1, response.tools().size());
        assertEquals("echo", response.tools().get(0).name());
        assertEquals("Echoes input", response.tools().get(0).description());
    }

    @SuppressWarnings("unchecked")
    @Test
    void testDiscoverTools_unreachableServer_http401() throws Exception {
        Map<String, Object> row = buildRow(SERVER_ID, "my-server", "http://localhost:8080/mcp", "bearer_token", "badtoken", "active");
        when(repository.findById(TENANT_ID, SERVER_ID)).thenReturn(Optional.of(row));

        HttpClient mockHttpClient = mock(HttpClient.class);
        HttpResponse<String> initResponse = mock(HttpResponse.class);
        when(initResponse.statusCode()).thenReturn(401);

        when(mockHttpClient.send(any(HttpRequest.class), any(HttpResponse.BodyHandler.class)))
            .thenReturn(initResponse);

        ToolServerService serviceWithMockHttp = new ToolServerService(repository, objectMapper, mockHttpClient);
        ToolDiscoverResponse response = serviceWithMockHttp.discoverTools(SERVER_ID);

        assertEquals("unreachable", response.status());
        assertNotNull(response.error());
        assertTrue(response.error().contains("401"));
        assertTrue(response.tools().isEmpty());
    }

    @SuppressWarnings("unchecked")
    @Test
    void testDiscoverTools_jsonRpcError_returnsUnreachable() throws Exception {
        Map<String, Object> row = buildRow(SERVER_ID, "my-server", "http://localhost:8080/mcp", "none", null, "active");
        when(repository.findById(TENANT_ID, SERVER_ID)).thenReturn(Optional.of(row));

        HttpClient mockHttpClient = mock(HttpClient.class);
        HttpResponse<String> initResponse = mock(HttpResponse.class);
        when(initResponse.statusCode()).thenReturn(200);
        when(initResponse.headers()).thenReturn(java.net.http.HttpHeaders.of(Map.of(), (a, b) -> true));

        HttpResponse<String> listResponse = mock(HttpResponse.class);
        when(listResponse.statusCode()).thenReturn(200);
        when(listResponse.body()).thenReturn(
            "{\"jsonrpc\":\"2.0\",\"id\":2,\"error\":{\"code\":-32601,\"message\":\"Method not found\"}}"
        );

        when(mockHttpClient.send(any(HttpRequest.class), any(HttpResponse.BodyHandler.class)))
            .thenReturn(initResponse, listResponse);

        ToolServerService serviceWithMockHttp = new ToolServerService(repository, objectMapper, mockHttpClient);
        ToolDiscoverResponse response = serviceWithMockHttp.discoverTools(SERVER_ID);

        assertEquals("unreachable", response.status());
        assertEquals("Method not found", response.error());
        assertTrue(response.tools().isEmpty());
    }
}
