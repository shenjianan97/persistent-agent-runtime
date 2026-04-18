package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;

import java.io.IOException;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doReturn;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class DefaultMemoryEmbeddingClientTest {

    private JdbcTemplate jdbcTemplate;
    private HttpClient httpClient;
    private ObjectMapper objectMapper;

    @BeforeEach
    void setUp() {
        jdbcTemplate = mock(JdbcTemplate.class);
        httpClient = mock(HttpClient.class);
        objectMapper = new ObjectMapper();
    }

    @Test
    void embedQuery_happyPath_returnsVector() throws Exception {
        when(jdbcTemplate.queryForList(anyString(), anyString()))
                .thenReturn(List.of(Map.of("api_key", "sk-test")));

        // Build a 1536-dim stub response.
        StringBuilder vec = new StringBuilder();
        vec.append("[");
        for (int i = 0; i < 1536; i++) {
            if (i > 0) vec.append(',');
            vec.append("0.0");
        }
        vec.append("]");
        String body = "{\"data\":[{\"embedding\":" + vec
                + "}],\"usage\":{\"total_tokens\":7}}";

        HttpResponse<String> response = stubResponse(200, body);
        doReturn(response).when(httpClient).send(any(HttpRequest.class), any());

        DefaultMemoryEmbeddingClient client = new DefaultMemoryEmbeddingClient(
                jdbcTemplate, objectMapper, "openai",
                "https://api.openai.com/v1/embeddings",
                "text-embedding-3-small",
                httpClient);

        MemoryEmbeddingClient.EmbeddingResult result = client.embedQuery("hi");
        assertThat(result.vector()).hasSize(1536);
        assertThat(result.tokens()).isEqualTo(7);
        assertThat(result.modelId()).isEqualTo("text-embedding-3-small");
    }

    @Test
    void embedQuery_missingKey_raises() {
        when(jdbcTemplate.queryForList(anyString(), anyString())).thenReturn(List.of());
        DefaultMemoryEmbeddingClient client = new DefaultMemoryEmbeddingClient(
                jdbcTemplate, objectMapper, "openai",
                "https://api.openai.com/v1/embeddings",
                "text-embedding-3-small",
                httpClient);
        assertThatThrownBy(() -> client.embedQuery("x"))
                .isInstanceOf(MemoryEmbeddingClient.EmbeddingUnavailableException.class);
    }

    @Test
    void embedQuery_providerReturnsHttp500_retriesOnceThenRaises() throws Exception {
        when(jdbcTemplate.queryForList(anyString(), anyString()))
                .thenReturn(List.of(Map.of("api_key", "sk-test")));
        HttpResponse<String> fail = stubResponse(500, "internal");
        doReturn(fail).when(httpClient).send(any(HttpRequest.class), any());

        DefaultMemoryEmbeddingClient client = new DefaultMemoryEmbeddingClient(
                jdbcTemplate, objectMapper, "openai",
                "https://api.openai.com/v1/embeddings",
                "text-embedding-3-small",
                httpClient);
        assertThatThrownBy(() -> client.embedQuery("x"))
                .isInstanceOf(MemoryEmbeddingClient.EmbeddingUnavailableException.class);
    }

    @Test
    void embedQuery_networkIOException_raises() throws Exception {
        when(jdbcTemplate.queryForList(anyString(), anyString()))
                .thenReturn(List.of(Map.of("api_key", "sk-test")));
        doThrow(new IOException("refused")).when(httpClient).send(any(HttpRequest.class), any());
        DefaultMemoryEmbeddingClient client = new DefaultMemoryEmbeddingClient(
                jdbcTemplate, objectMapper, "openai",
                "https://api.openai.com/v1/embeddings",
                "text-embedding-3-small",
                httpClient);
        assertThatThrownBy(() -> client.embedQuery("x"))
                .isInstanceOf(MemoryEmbeddingClient.EmbeddingUnavailableException.class);
    }

    @Test
    void embedQuery_wrongDimension_raises() throws Exception {
        when(jdbcTemplate.queryForList(anyString(), anyString()))
                .thenReturn(List.of(Map.of("api_key", "sk-test")));
        String body = "{\"data\":[{\"embedding\":[0.1,0.2]}],\"usage\":{\"total_tokens\":1}}";
        doReturn(stubResponse(200, body)).when(httpClient).send(any(HttpRequest.class), any());
        DefaultMemoryEmbeddingClient client = new DefaultMemoryEmbeddingClient(
                jdbcTemplate, objectMapper, "openai",
                "https://api.openai.com/v1/embeddings",
                "text-embedding-3-small",
                httpClient);
        assertThatThrownBy(() -> client.embedQuery("x"))
                .isInstanceOf(MemoryEmbeddingClient.EmbeddingUnavailableException.class)
                .hasRootCauseMessage("Embedding dimension mismatch: expected 1536, got 2");
    }

    @SuppressWarnings("unchecked")
    private static HttpResponse<String> stubResponse(int status, String body) {
        HttpResponse<String> response = mock(HttpResponse.class);
        when(response.statusCode()).thenReturn(status);
        when(response.body()).thenReturn(body);
        return response;
    }
}
