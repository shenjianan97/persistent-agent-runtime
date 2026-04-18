package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * Default {@link MemoryEmbeddingClient} backed by an OpenAI-compatible
 * {@code /v1/embeddings} endpoint. Reads the API key from the existing
 * {@code provider_keys} row (Task 5 will extend model-discovery startup to
 * validate this key, but at runtime the row presence is sufficient).
 *
 * <p>Bounded: a single-shot HTTPS call per query with a short timeout and
 * 1 retry. On any failure it raises {@link EmbeddingUnavailableException};
 * the service layer interprets the result per the design doc's degrade rules.
 */
@Component
public class DefaultMemoryEmbeddingClient implements MemoryEmbeddingClient {

    private static final Logger log = LoggerFactory.getLogger(DefaultMemoryEmbeddingClient.class);

    /** Default model per design doc "Embeddings" section. */
    static final String DEFAULT_MODEL = "text-embedding-3-small";

    /** Expected embedding dimension matching {@code vector(1536)} column. */
    static final int EMBEDDING_DIMENSION = 1536;

    /** Short timeout per design doc (&lt;= 5s bounded). */
    private static final Duration REQUEST_TIMEOUT = Duration.ofMillis(5_000);

    /** 1 retry per design doc. */
    private static final int MAX_ATTEMPTS = 2;

    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;
    private final HttpClient httpClient;
    private final String providerId;
    private final String endpointUrl;
    private final String modelId;

    @org.springframework.beans.factory.annotation.Autowired
    public DefaultMemoryEmbeddingClient(
            JdbcTemplate jdbcTemplate,
            ObjectMapper objectMapper,
            @Value("${app.memory.embedding.provider-id:openai}") String providerId,
            @Value("${app.memory.embedding.endpoint:https://api.openai.com/v1/embeddings}") String endpointUrl,
            @Value("${app.memory.embedding.model:" + DEFAULT_MODEL + "}") String modelId) {
        this(jdbcTemplate, objectMapper, providerId, endpointUrl, modelId,
                HttpClient.newBuilder().connectTimeout(REQUEST_TIMEOUT).build());
    }

    // Package-private constructor for testing.
    DefaultMemoryEmbeddingClient(
            JdbcTemplate jdbcTemplate,
            ObjectMapper objectMapper,
            String providerId,
            String endpointUrl,
            String modelId,
            HttpClient httpClient) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
        this.providerId = providerId;
        this.endpointUrl = endpointUrl;
        this.modelId = modelId;
        this.httpClient = httpClient;
    }

    @Override
    public EmbeddingResult embedQuery(String query) {
        String apiKey = lookupApiKey();
        if (apiKey == null || apiKey.isBlank()) {
            throw new EmbeddingUnavailableException(
                    "Embedding provider key not configured (provider_id=" + providerId + ")");
        }

        String body;
        try {
            body = objectMapper.writeValueAsString(Map.of(
                    "model", modelId,
                    "input", query));
        } catch (Exception e) {
            throw new EmbeddingUnavailableException(
                    "Failed to serialize embedding request", e);
        }

        Throwable lastFailure = null;
        for (int attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            try {
                HttpRequest request = HttpRequest.newBuilder()
                        .uri(URI.create(endpointUrl))
                        .timeout(REQUEST_TIMEOUT)
                        .header("Content-Type", "application/json")
                        .header("Authorization", "Bearer " + apiKey)
                        .POST(HttpRequest.BodyPublishers.ofString(body))
                        .build();
                HttpResponse<String> response = httpClient.send(
                        request, HttpResponse.BodyHandlers.ofString());
                if (response.statusCode() >= 400) {
                    throw new EmbeddingUnavailableException(
                            "Embedding provider returned HTTP " + response.statusCode());
                }
                return parseResponse(response.body());
            } catch (EmbeddingUnavailableException e) {
                lastFailure = e;
                log.warn("embedding attempt {} failed: {}", attempt, e.getMessage());
            } catch (Exception e) {
                lastFailure = e;
                log.warn("embedding attempt {} failed: {}", attempt, e.getMessage());
            }
        }
        throw new EmbeddingUnavailableException(
                "Embedding provider unavailable after " + MAX_ATTEMPTS + " attempt(s)",
                lastFailure);
    }

    private String lookupApiKey() {
        List<Map<String, Object>> rows = jdbcTemplate.queryForList(
                "SELECT api_key FROM provider_keys WHERE provider_id = ?",
                providerId);
        if (rows.isEmpty()) {
            return null;
        }
        return (String) rows.get(0).get("api_key");
    }

    private EmbeddingResult parseResponse(String body) {
        try {
            JsonNode root = objectMapper.readTree(body);
            JsonNode dataNode = root.path("data");
            if (!dataNode.isArray() || dataNode.isEmpty()) {
                throw new EmbeddingUnavailableException("Embedding response missing 'data'");
            }
            JsonNode embeddingNode = dataNode.get(0).path("embedding");
            if (!embeddingNode.isArray() || embeddingNode.size() != EMBEDDING_DIMENSION) {
                throw new EmbeddingUnavailableException(
                        "Embedding dimension mismatch: expected " + EMBEDDING_DIMENSION
                                + ", got " + embeddingNode.size());
            }
            float[] vector = new float[EMBEDDING_DIMENSION];
            for (int i = 0; i < EMBEDDING_DIMENSION; i++) {
                vector[i] = (float) embeddingNode.get(i).asDouble();
            }
            int tokens = root.path("usage").path("total_tokens").asInt(0);
            long costMicrodollars = 0L;
            return new EmbeddingResult(vector, tokens, costMicrodollars, modelId);
        } catch (EmbeddingUnavailableException e) {
            throw e;
        } catch (Exception e) {
            throw new EmbeddingUnavailableException("Failed to parse embedding response", e);
        }
    }
}
