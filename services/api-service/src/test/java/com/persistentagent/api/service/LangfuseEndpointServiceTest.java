package com.persistentagent.api.service;

import com.persistentagent.api.model.request.LangfuseEndpointRequest;
import com.persistentagent.api.model.response.LangfuseEndpointResponse;
import com.persistentagent.api.model.response.LangfuseEndpointTestResponse;
import com.persistentagent.api.repository.LangfuseEndpointRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.MockedStatic;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.dao.DuplicateKeyException;

import java.io.IOException;
import java.net.ConnectException;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class LangfuseEndpointServiceTest {

    @Mock
    private LangfuseEndpointRepository langfuseEndpointRepository;

    @Mock
    private HttpClient httpClient;

    private LangfuseEndpointService langfuseEndpointService;

    private static final String TENANT_ID = "default";
    private static final UUID ENDPOINT_ID = UUID.randomUUID();

    @BeforeEach
    void setUp() {
        langfuseEndpointService = new LangfuseEndpointService(langfuseEndpointRepository, httpClient);
    }

    // --- create tests ---

    @Test
    void create_success() {
        LangfuseEndpointRequest request = new LangfuseEndpointRequest(
                "my-langfuse", "https://langfuse.example.com", "pk-123", "sk-456");

        Map<String, Object> repoResult = new LinkedHashMap<>();
        repoResult.put("endpoint_id", ENDPOINT_ID);
        repoResult.put("created_at", Timestamp.from(Instant.now()));

        when(langfuseEndpointRepository.insert(TENANT_ID, "my-langfuse",
                "https://langfuse.example.com", "pk-123", "sk-456")).thenReturn(repoResult);

        LangfuseEndpointResponse response = langfuseEndpointService.create(TENANT_ID, request);

        assertNotNull(response);
        assertEquals(ENDPOINT_ID, response.endpointId());
        assertEquals(TENANT_ID, response.tenantId());
        assertEquals("my-langfuse", response.name());
        assertEquals("https://langfuse.example.com", response.host());
        assertNotNull(response.createdAt());
    }

    @Test
    void create_duplicateName_throws409() {
        LangfuseEndpointRequest request = new LangfuseEndpointRequest(
                "duplicate-name", "https://langfuse.example.com", "pk-123", "sk-456");

        when(langfuseEndpointRepository.insert(anyString(), anyString(), anyString(), anyString(), anyString()))
                .thenThrow(new DuplicateKeyException("duplicate key value violates unique constraint"));

        assertThrows(LangfuseEndpointService.ConflictException.class,
                () -> langfuseEndpointService.create(TENANT_ID, request));
    }

    // --- get tests ---

    @Test
    void get_notFound_throws404() {
        when(langfuseEndpointRepository.findByIdAndTenant(ENDPOINT_ID, TENANT_ID))
                .thenReturn(Optional.empty());

        assertThrows(LangfuseEndpointService.NotFoundException.class,
                () -> langfuseEndpointService.get(ENDPOINT_ID, TENANT_ID));
    }

    // --- delete tests ---

    @Test
    void delete_success() {
        when(langfuseEndpointRepository.isReferencedByActiveTask(ENDPOINT_ID)).thenReturn(false);
        when(langfuseEndpointRepository.delete(ENDPOINT_ID, TENANT_ID)).thenReturn(true);

        assertDoesNotThrow(() -> langfuseEndpointService.delete(ENDPOINT_ID, TENANT_ID));
        verify(langfuseEndpointRepository).delete(ENDPOINT_ID, TENANT_ID);
    }

    @Test
    void delete_referenced_throws409() {
        when(langfuseEndpointRepository.isReferencedByActiveTask(ENDPOINT_ID)).thenReturn(true);

        assertThrows(LangfuseEndpointService.ConflictException.class,
                () -> langfuseEndpointService.delete(ENDPOINT_ID, TENANT_ID));
        verify(langfuseEndpointRepository, never()).delete(any(), anyString());
    }

    // --- testConnectivity tests ---

    @Test
    @SuppressWarnings("unchecked")
    void testConnectivity_success() throws Exception {
        Map<String, Object> row = buildEndpointRow("https://langfuse.example.com", "pk-123", "sk-456");
        when(langfuseEndpointRepository.findByIdAndTenant(ENDPOINT_ID, TENANT_ID))
                .thenReturn(Optional.of(row));

        HttpResponse<String> mockHttpResponse = mock(HttpResponse.class);
        when(mockHttpResponse.statusCode()).thenReturn(200);
        when(httpClient.send(any(HttpRequest.class), any(HttpResponse.BodyHandler.class)))
                .thenReturn(mockHttpResponse);

        LangfuseEndpointTestResponse response = langfuseEndpointService.testConnectivity(ENDPOINT_ID, TENANT_ID);

        assertTrue(response.reachable());
        assertEquals("OK", response.message());
    }

    @Test
    @SuppressWarnings("unchecked")
    void testConnectivity_authFailure() throws Exception {
        Map<String, Object> row = buildEndpointRow("https://langfuse.example.com", "pk-bad", "sk-bad");
        when(langfuseEndpointRepository.findByIdAndTenant(ENDPOINT_ID, TENANT_ID))
                .thenReturn(Optional.of(row));

        HttpResponse<String> mockHttpResponse = mock(HttpResponse.class);
        when(mockHttpResponse.statusCode()).thenReturn(401);
        when(httpClient.send(any(HttpRequest.class), any(HttpResponse.BodyHandler.class)))
                .thenReturn(mockHttpResponse);

        LangfuseEndpointTestResponse response = langfuseEndpointService.testConnectivity(ENDPOINT_ID, TENANT_ID);

        assertFalse(response.reachable());
        assertTrue(response.message().contains("Authentication failed"));
    }

    @Test
    @SuppressWarnings("unchecked")
    void testConnectivity_unreachable() throws Exception {
        Map<String, Object> row = buildEndpointRow("https://unreachable.example.com", "pk-123", "sk-456");
        when(langfuseEndpointRepository.findByIdAndTenant(ENDPOINT_ID, TENANT_ID))
                .thenReturn(Optional.of(row));

        when(httpClient.send(any(HttpRequest.class), any(HttpResponse.BodyHandler.class)))
                .thenThrow(new ConnectException("Connection refused"));

        LangfuseEndpointTestResponse response = langfuseEndpointService.testConnectivity(ENDPOINT_ID, TENANT_ID);

        assertFalse(response.reachable());
        assertTrue(response.message().contains("Cannot reach host"));
    }

    // --- helpers ---

    private Map<String, Object> buildEndpointRow(String host, String publicKey, String secretKey) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("endpoint_id", ENDPOINT_ID);
        row.put("tenant_id", TENANT_ID);
        row.put("name", "my-langfuse");
        row.put("host", host);
        row.put("public_key", publicKey);
        row.put("secret_key", secretKey);
        row.put("created_at", Timestamp.from(Instant.now()));
        row.put("updated_at", null);
        return row;
    }
}
