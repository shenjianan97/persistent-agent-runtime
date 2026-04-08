package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.ToolServerNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.ToolServerCreateRequest;
import com.persistentagent.api.model.request.ToolServerUpdateRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.repository.ToolServerRepository;
import com.persistentagent.api.util.DateTimeUtil;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.*;

@Service
public class ToolServerService {

    private final ToolServerRepository repository;
    private final HttpClient httpClient;

    public ToolServerService(ToolServerRepository repository) {
        this.repository = repository;
        this.httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofMillis(ValidationConstants.TOOL_SERVER_DISCOVER_TIMEOUT_MS))
            .build();
    }

    @Transactional
    public ToolServerResponse createToolServer(ToolServerCreateRequest request) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        String authType = request.authType() != null ? request.authType() : ValidationConstants.TOOL_SERVER_AUTH_NONE;

        validateAuthType(authType);
        validateAuthToken(authType, request.authToken());
        validateUrl(request.url());

        // Clear auth_token if auth_type is 'none'
        String effectiveToken = ValidationConstants.TOOL_SERVER_AUTH_NONE.equals(authType) ? null : request.authToken();

        try {
            Map<String, Object> row = repository.insert(
                tenantId, request.name(), request.url(), authType, effectiveToken
            );
            return toMaskedResponse(row);
        } catch (DuplicateKeyException e) {
            throw new ValidationException("A tool server with name '" + request.name() + "' already exists");
        }
    }

    public List<ToolServerSummaryResponse> listToolServers(String status, Integer limit) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        if (status != null && !status.isBlank() && !ValidationConstants.VALID_TOOL_SERVER_STATUSES.contains(status)) {
            throw new ValidationException("Invalid status filter: " + status + ". Must be one of: " + ValidationConstants.VALID_TOOL_SERVER_STATUSES);
        }
        int effectiveLimit = limit != null
            ? Math.max(1, Math.min(limit, ValidationConstants.MAX_TOOL_SERVER_LIST_LIMIT))
            : ValidationConstants.DEFAULT_TOOL_SERVER_LIST_LIMIT;

        return repository.listByTenant(tenantId, status, effectiveLimit).stream()
            .map(this::toSummaryResponse)
            .toList();
    }

    public ToolServerResponse getToolServer(String serverId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        Map<String, Object> row = repository.findById(tenantId, serverId)
            .orElseThrow(() -> new ToolServerNotFoundException(serverId));
        return toMaskedResponse(row);
    }

    @Transactional
    public ToolServerResponse updateToolServer(String serverId, ToolServerUpdateRequest request) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        if (request.authType() != null) {
            validateAuthType(request.authType());
        }
        if (request.status() != null && !ValidationConstants.VALID_TOOL_SERVER_STATUSES.contains(request.status())) {
            throw new ValidationException("Invalid status: " + request.status() + ". Must be one of: " + ValidationConstants.VALID_TOOL_SERVER_STATUSES);
        }
        if (request.url() != null) {
            validateUrl(request.url());
        }

        // Clear auth_token if auth_type is being changed to 'none'
        String effectiveToken = request.authToken();
        if (ValidationConstants.TOOL_SERVER_AUTH_NONE.equals(request.authType())) {
            effectiveToken = "";  // Non-null empty string signals "clear the token"
        }

        try {
            Map<String, Object> row = repository.update(
                tenantId, serverId,
                request.name(), request.url(),
                request.authType(), effectiveToken,
                request.status()
            ).orElseThrow(() -> new ToolServerNotFoundException(serverId));
            return toMaskedResponse(row);
        } catch (DuplicateKeyException e) {
            throw new ValidationException("A tool server with name '" + request.name() + "' already exists");
        }
    }

    public void deleteToolServer(String serverId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        boolean deleted = repository.delete(tenantId, serverId);
        if (!deleted) {
            throw new ToolServerNotFoundException(serverId);
        }
    }

    public ToolDiscoverResponse discoverTools(String serverId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        Map<String, Object> row = repository.findById(tenantId, serverId)
            .orElseThrow(() -> new ToolServerNotFoundException(serverId));

        String serverName = (String) row.get("name");
        String serverUrl = (String) row.get("url");
        String authType = (String) row.get("auth_type");
        String authToken = (String) row.get("auth_token");

        try {
            // Step 1: Send MCP initialize request
            String initBody = """
                {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"persistent-agent-runtime","version":"1.0.0"}}}
                """.strip();

            HttpRequest.Builder initReqBuilder = HttpRequest.newBuilder()
                .uri(URI.create(serverUrl))
                .timeout(Duration.ofMillis(ValidationConstants.TOOL_SERVER_DISCOVER_TIMEOUT_MS))
                .header("Content-Type", "application/json")
                .header("Accept", "application/json, text/event-stream")
                .POST(HttpRequest.BodyPublishers.ofString(initBody));

            if (ValidationConstants.TOOL_SERVER_AUTH_BEARER.equals(authType) && authToken != null) {
                initReqBuilder.header("Authorization", "Bearer " + authToken);
            }

            HttpResponse<String> initResp = httpClient.send(initReqBuilder.build(), HttpResponse.BodyHandlers.ofString());

            // Extract session ID from response header if present
            String sessionId = initResp.headers().firstValue("mcp-session-id").orElse(null);

            // Step 2: Send tools/list request
            String listBody = """
                {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
                """.strip();

            HttpRequest.Builder listReqBuilder = HttpRequest.newBuilder()
                .uri(URI.create(serverUrl))
                .timeout(Duration.ofMillis(ValidationConstants.TOOL_SERVER_DISCOVER_TIMEOUT_MS))
                .header("Content-Type", "application/json")
                .header("Accept", "application/json, text/event-stream")
                .POST(HttpRequest.BodyPublishers.ofString(listBody));

            if (ValidationConstants.TOOL_SERVER_AUTH_BEARER.equals(authType) && authToken != null) {
                listReqBuilder.header("Authorization", "Bearer " + authToken);
            }
            if (sessionId != null) {
                listReqBuilder.header("Mcp-Session-Id", sessionId);
            }

            HttpResponse<String> listResp = httpClient.send(listReqBuilder.build(), HttpResponse.BodyHandlers.ofString());

            // Parse the JSON-RPC response to extract tools
            List<DiscoveredToolInfo> tools = parseToolsFromResponse(listResp.body());

            return new ToolDiscoverResponse(
                row.get("server_id").toString(), serverName, "reachable", null, tools
            );

        } catch (Exception e) {
            return new ToolDiscoverResponse(
                row.get("server_id").toString(), serverName, "unreachable",
                e.getMessage(), List.of()
            );
        }
    }

    // --- Private helpers ---

    private List<DiscoveredToolInfo> parseToolsFromResponse(String responseBody) {
        // The response may be SSE (text/event-stream) or plain JSON
        // For SSE, extract the data line containing the JSON-RPC result
        String jsonBody = responseBody;
        if (responseBody.contains("data:")) {
            // SSE format: find last data: line with JSON content
            String[] lines = responseBody.split("\n");
            for (String line : lines) {
                if (line.startsWith("data:") && line.contains("tools/list")) {
                    jsonBody = line.substring(5).trim();
                    break;
                }
                if (line.startsWith("data:") && line.contains("\"tools\"")) {
                    jsonBody = line.substring(5).trim();
                    break;
                }
            }
        }

        try {
            com.fasterxml.jackson.databind.ObjectMapper mapper = new com.fasterxml.jackson.databind.ObjectMapper();
            com.fasterxml.jackson.databind.JsonNode root = mapper.readTree(jsonBody);
            com.fasterxml.jackson.databind.JsonNode toolsNode = root.path("result").path("tools");
            if (!toolsNode.isArray()) {
                return List.of();
            }

            List<DiscoveredToolInfo> tools = new ArrayList<>();
            for (com.fasterxml.jackson.databind.JsonNode toolNode : toolsNode) {
                String name = toolNode.path("name").asText("");
                String description = toolNode.path("description").asText("");
                Object inputSchema = toolNode.has("inputSchema")
                    ? mapper.treeToValue(toolNode.get("inputSchema"), Object.class)
                    : null;
                tools.add(new DiscoveredToolInfo(name, description, inputSchema));
            }
            return tools;
        } catch (Exception e) {
            return List.of();
        }
    }

    private void validateAuthType(String authType) {
        if (!ValidationConstants.VALID_TOOL_SERVER_AUTH_TYPES.contains(authType)) {
            throw new ValidationException("Invalid auth_type: " + authType + ". Must be one of: " + ValidationConstants.VALID_TOOL_SERVER_AUTH_TYPES);
        }
    }

    private void validateAuthToken(String authType, String authToken) {
        if (ValidationConstants.TOOL_SERVER_AUTH_BEARER.equals(authType) && (authToken == null || authToken.isBlank())) {
            throw new ValidationException("auth_token is required when auth_type is 'bearer_token'");
        }
    }

    private void validateUrl(String url) {
        try {
            URI uri = URI.create(url);
            String scheme = uri.getScheme();
            if (scheme == null || (!scheme.equals("http") && !scheme.equals("https"))) {
                throw new ValidationException("url must use http or https scheme");
            }
        } catch (IllegalArgumentException e) {
            throw new ValidationException("Invalid url: " + e.getMessage());
        }
    }

    private String maskToken(String token) {
        if (token == null || token.length() <= 8) {
            return token != null ? "****" : null;
        }
        return token.substring(0, 4) + "..." + token.substring(token.length() - 4);
    }

    private ToolServerResponse toMaskedResponse(Map<String, Object> row) {
        ToolServerResponse full = toResponse(row);
        return new ToolServerResponse(
            full.serverId(), full.tenantId(), full.name(), full.url(),
            full.authType(), maskToken(full.authToken()), full.status(),
            full.createdAt(), full.updatedAt()
        );
    }

    private ToolServerResponse toResponse(Map<String, Object> row) {
        return new ToolServerResponse(
            row.get("server_id").toString(),
            (String) row.get("tenant_id"),
            (String) row.get("name"),
            (String) row.get("url"),
            (String) row.get("auth_type"),
            (String) row.get("auth_token"),
            (String) row.get("status"),
            DateTimeUtil.toOffsetDateTime(row.get("created_at")),
            DateTimeUtil.toOffsetDateTime(row.get("updated_at"))
        );
    }

    private ToolServerSummaryResponse toSummaryResponse(Map<String, Object> row) {
        return new ToolServerSummaryResponse(
            row.get("server_id").toString(),
            (String) row.get("tenant_id"),
            (String) row.get("name"),
            (String) row.get("url"),
            (String) row.get("auth_type"),
            (String) row.get("status"),
            DateTimeUtil.toOffsetDateTime(row.get("created_at")),
            DateTimeUtil.toOffsetDateTime(row.get("updated_at"))
        );
    }
}
