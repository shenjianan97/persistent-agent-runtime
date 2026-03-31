package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.LangfuseEndpointRequest;
import com.persistentagent.api.model.response.LangfuseEndpointResponse;
import com.persistentagent.api.model.response.LangfuseEndpointTestResponse;
import com.persistentagent.api.repository.LangfuseEndpointRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Service
public class LangfuseEndpointService {

    private static final Logger log = LoggerFactory.getLogger(LangfuseEndpointService.class);

    private final LangfuseEndpointRepository langfuseEndpointRepository;
    private final HttpClient httpClient;

    @Autowired
    public LangfuseEndpointService(LangfuseEndpointRepository langfuseEndpointRepository) {
        this(langfuseEndpointRepository, HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofSeconds(5))
                .build());
    }

    // Package-visible constructor for testing
    LangfuseEndpointService(LangfuseEndpointRepository langfuseEndpointRepository, HttpClient httpClient) {
        this.langfuseEndpointRepository = langfuseEndpointRepository;
        this.httpClient = httpClient;
    }

    public LangfuseEndpointResponse create(String tenantId, LangfuseEndpointRequest request) {
        LangfuseEndpointTestResponse testResult = doTestConnectivity(request.host(), request.publicKey(), request.secretKey());
        if (!testResult.reachable()) {
            throw new ConnectivityException(testResult.message());
        }
        try {
            Map<String, Object> result = langfuseEndpointRepository.insert(
                    tenantId, request.name(), request.host(),
                    request.publicKey(), request.secretKey());
            UUID endpointId = (UUID) result.get("endpoint_id");
            Instant createdAt = toInstant(result.get("created_at"));
            return new LangfuseEndpointResponse(endpointId, tenantId, request.name(), request.host(), createdAt, null);
        } catch (DuplicateKeyException e) {
            throw new ConflictException("A Langfuse endpoint with this name already exists for the tenant");
        }
    }

    public List<LangfuseEndpointResponse> list(String tenantId) {
        return langfuseEndpointRepository.listByTenant(tenantId).stream()
                .map(row -> toResponse(tenantId, row))
                .toList();
    }

    public LangfuseEndpointResponse get(UUID endpointId, String tenantId) {
        return langfuseEndpointRepository.findByIdAndTenant(endpointId, tenantId)
                .map(row -> toResponse(tenantId, row))
                .orElseThrow(() -> new NotFoundException("Langfuse endpoint not found: " + endpointId));
    }

    public LangfuseEndpointResponse update(UUID endpointId, String tenantId, LangfuseEndpointRequest request) {
        LangfuseEndpointTestResponse testResult = doTestConnectivity(request.host(), request.publicKey(), request.secretKey());
        if (!testResult.reachable()) {
            throw new ConnectivityException(testResult.message());
        }
        try {
            boolean updated = langfuseEndpointRepository.update(
                    endpointId, tenantId, request.name(), request.host(),
                    request.publicKey(), request.secretKey());
            if (!updated) {
                throw new NotFoundException("Langfuse endpoint not found: " + endpointId);
            }
        } catch (org.springframework.dao.DuplicateKeyException e) {
            throw new ConflictException("A Langfuse endpoint with this name already exists for the tenant");
        }
        // Re-fetch to get updated_at
        return langfuseEndpointRepository.findByIdAndTenant(endpointId, tenantId)
                .map(row -> toResponse(tenantId, row))
                .orElseThrow(() -> new NotFoundException("Langfuse endpoint not found: " + endpointId));
    }

    public void delete(UUID endpointId, String tenantId) {
        // Check if referenced by active tasks first
        if (langfuseEndpointRepository.isReferencedByActiveTask(endpointId)) {
            throw new ConflictException("Langfuse endpoint is referenced by active tasks (queued or running) and cannot be deleted");
        }
        boolean deleted = langfuseEndpointRepository.delete(endpointId, tenantId);
        if (!deleted) {
            throw new NotFoundException("Langfuse endpoint not found: " + endpointId);
        }
    }

    public LangfuseEndpointTestResponse testConnectivity(UUID endpointId, String tenantId) {
        Map<String, Object> row = langfuseEndpointRepository.findByIdAndTenant(endpointId, tenantId)
                .orElseThrow(() -> new NotFoundException("Langfuse endpoint not found: " + endpointId));

        String host = (String) row.get("host");
        String publicKey = (String) row.get("public_key");
        String secretKey = (String) row.get("secret_key");

        return doTestConnectivity(host, publicKey, secretKey);
    }

    private LangfuseEndpointTestResponse doTestConnectivity(String host, String publicKey, String secretKey) {
        String url = host.endsWith("/") ? host + "api/public/health" : host + "/api/public/health";
        String credentials = Base64.getEncoder().encodeToString((publicKey + ":" + secretKey).getBytes());

        try {
            HttpRequest httpRequest = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .header("Authorization", "Basic " + credentials)
                    .timeout(Duration.ofSeconds(5))
                    .GET()
                    .build();

            HttpResponse<String> response = httpClient.send(httpRequest, HttpResponse.BodyHandlers.ofString());
            int statusCode = response.statusCode();

            if (statusCode >= 200 && statusCode < 300) {
                return new LangfuseEndpointTestResponse(true, "OK");
            } else if (statusCode == 401 || statusCode == 403) {
                return new LangfuseEndpointTestResponse(false, "Authentication failed — check public key and secret key");
            } else {
                return new LangfuseEndpointTestResponse(false, "Unexpected status: " + statusCode);
            }
        } catch (java.net.http.HttpTimeoutException | java.net.ConnectException e) {
            return new LangfuseEndpointTestResponse(false, "Cannot reach host — check URL");
        } catch (Exception e) {
            log.warn("Connectivity test failed for host {}: {}", host, e.getMessage());
            return new LangfuseEndpointTestResponse(false, "Cannot reach host — check URL");
        }
    }

    // --- Conversion helpers ---

    private LangfuseEndpointResponse toResponse(String tenantId, Map<String, Object> row) {
        return new LangfuseEndpointResponse(
                (UUID) row.get("endpoint_id"),
                tenantId,
                (String) row.get("name"),
                (String) row.get("host"),
                toInstant(row.get("created_at")),
                toInstant(row.get("updated_at")));
    }

    private Instant toInstant(Object value) {
        if (value == null) return null;
        if (value instanceof Instant i) return i;
        if (value instanceof Timestamp ts) return ts.toInstant();
        if (value instanceof java.util.Date d) return d.toInstant();
        return null;
    }

    // --- Inner exception types (package-visible) ---

    public static class NotFoundException extends RuntimeException {
        public NotFoundException(String message) {
            super(message);
        }
    }

    public static class ConflictException extends RuntimeException {
        public ConflictException(String message) {
            super(message);
        }
    }

    public static class ConnectivityException extends RuntimeException {
        public ConnectivityException(String message) {
            super(message);
        }
    }
}
