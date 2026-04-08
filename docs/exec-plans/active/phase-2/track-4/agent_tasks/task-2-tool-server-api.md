<!-- AGENT_TASK_START: task-2-tool-server-api.md -->

# Task 2 — Tool Server CRUD API + Discover Endpoint

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` — canonical design contract (API Design section)
2. `services/api-service/src/main/java/com/persistentagent/api/controller/AgentController.java` — existing CRUD pattern
3. `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` — service layer pattern
4. `services/api-service/src/main/java/com/persistentagent/api/repository/AgentRepository.java` — repository pattern (JdbcTemplate)
5. `services/api-service/src/main/java/com/persistentagent/api/config/ValidationConstants.java` — validation constants
6. `services/api-service/src/main/java/com/persistentagent/api/exception/GlobalExceptionHandler.java` — error handling pattern
7. `infrastructure/database/migrations/0008_tool_servers.sql` — Task 1 output: `tool_servers` table schema

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-4/progress.md` to "Done".

## Context

Track 4 requires a REST API for managing external MCP tool server registrations. This follows the same controller → service → repository pattern established by the Agent CRUD API in Track 1. The API supports full CRUD operations plus a `discover` endpoint that probes an MCP server and returns its tool list.

The discover endpoint requires an MCP client dependency. Spring Boot's HTTP client can be used to make a basic probe, but for full MCP protocol compliance (initialize + tools/list), the discover endpoint should use an HTTP-based MCP protocol exchange. For Track 4, a lightweight approach is acceptable: make an HTTP POST to the server's URL with the MCP `initialize` and `tools/list` JSON-RPC messages.

## Task-Specific Shared Contract

- Treat `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` API Design section as the canonical contract.
- Server `name` must match `[a-z0-9][a-z0-9-]*` (validated at the API layer, enforced by DB constraint).
- `auth_token` is never returned in full in any API response. List responses omit it entirely; detail responses mask it (e.g., `"ghp_xxxx...xxxx"`).
- The discover endpoint (`POST /v1/tool-servers/{serverId}/discover`) connects to the MCP server, calls `tools/list`, and returns discovered tools. It does not persist anything.
- `tenant_id` defaults to `"default"` (from `ValidationConstants.DEFAULT_TENANT_ID`).
- Delete is a hard delete — the row is removed from the database.
- All response timestamps use ISO 8601 format with timezone (consistent with existing Agent responses).

## Affected Component

- **Service/Module:** API Service — Tool Server Management
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/controller/ToolServerController.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/service/ToolServerService.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/repository/ToolServerRepository.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/ToolServerCreateRequest.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/ToolServerUpdateRequest.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/ToolServerResponse.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/ToolServerSummaryResponse.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/ToolDiscoverResponse.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/response/DiscoveredToolInfo.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/exception/ToolServerNotFoundException.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/exception/GlobalExceptionHandler.java` (modify — add ToolServerNotFoundException handler)
  - `services/api-service/src/main/java/com/persistentagent/api/config/ValidationConstants.java` (modify — add tool server constants)
  - `services/api-service/src/test/java/com/persistentagent/api/controller/ToolServerControllerTest.java` (new)
  - `services/api-service/src/test/java/com/persistentagent/api/service/ToolServerServiceTest.java` (new)
- **Change type:** new code + minor modifications

## Dependencies

- **Must complete first:** Task 1 (Database Migration — `tool_servers` table must exist)
- **Provides output to:** Task 6 (Console — Tool Servers), Task 7 (Console — Agent Config needs list endpoint), Task 8 (Integration Tests)
- **Shared interfaces/contracts:** REST API contract at `/v1/tool-servers`

## Implementation Specification

### Step 1: Add tool server constants to ValidationConstants

Add to `services/api-service/src/main/java/com/persistentagent/api/config/ValidationConstants.java`:

```java
// Tool server constants
public static final String TOOL_SERVER_NAME_PATTERN = "^[a-z0-9][a-z0-9-]*$";
public static final String TOOL_SERVER_STATUS_ACTIVE = "active";
public static final String TOOL_SERVER_STATUS_DISABLED = "disabled";
public static final Set<String> VALID_TOOL_SERVER_STATUSES = Set.of(TOOL_SERVER_STATUS_ACTIVE, TOOL_SERVER_STATUS_DISABLED);
public static final String TOOL_SERVER_AUTH_NONE = "none";
public static final String TOOL_SERVER_AUTH_BEARER = "bearer_token";
public static final Set<String> VALID_TOOL_SERVER_AUTH_TYPES = Set.of(TOOL_SERVER_AUTH_NONE, TOOL_SERVER_AUTH_BEARER);
public static final int DEFAULT_TOOL_SERVER_LIST_LIMIT = 50;
public static final int MAX_TOOL_SERVER_LIST_LIMIT = 200;
public static final int TOOL_SERVER_DISCOVER_TIMEOUT_MS = 10000;
```

### Step 2: Create request models

**ToolServerCreateRequest.java:**

```java
package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record ToolServerCreateRequest(
    @NotBlank(message = "name is required")
    @Size(max = 100, message = "name must not exceed 100 characters")
    @Pattern(regexp = "^[a-z0-9][a-z0-9-]*$", message = "name must be lowercase alphanumeric with hyphens, not starting with a hyphen")
    String name,

    @NotBlank(message = "url is required")
    @Size(max = 2048, message = "url must not exceed 2048 characters")
    String url,

    @JsonProperty("auth_type")
    String authType,

    @JsonProperty("auth_token")
    String authToken
) {}
```

**ToolServerUpdateRequest.java:**

```java
package com.persistentagent.api.model.request;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.Size;

public record ToolServerUpdateRequest(
    @Size(max = 100, message = "name must not exceed 100 characters")
    @Pattern(regexp = "^[a-z0-9][a-z0-9-]*$", message = "name must be lowercase alphanumeric with hyphens, not starting with a hyphen")
    String name,

    @Size(max = 2048, message = "url must not exceed 2048 characters")
    String url,

    @JsonProperty("auth_type")
    String authType,

    @JsonProperty("auth_token")
    String authToken,

    String status
) {}
```

### Step 3: Create response models

**ToolServerResponse.java:**

```java
package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.time.OffsetDateTime;

public record ToolServerResponse(
    @JsonProperty("server_id") String serverId,
    @JsonProperty("tenant_id") String tenantId,
    String name,
    String url,
    @JsonProperty("auth_type") String authType,
    @JsonProperty("auth_token") String authToken,
    String status,
    @JsonProperty("created_at") OffsetDateTime createdAt,
    @JsonProperty("updated_at") OffsetDateTime updatedAt
) {}
```

**ToolServerSummaryResponse.java** (same as ToolServerResponse but always has `auth_token` as null):

```java
package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.time.OffsetDateTime;

public record ToolServerSummaryResponse(
    @JsonProperty("server_id") String serverId,
    @JsonProperty("tenant_id") String tenantId,
    String name,
    String url,
    @JsonProperty("auth_type") String authType,
    String status,
    @JsonProperty("created_at") OffsetDateTime createdAt,
    @JsonProperty("updated_at") OffsetDateTime updatedAt
) {}
```

**DiscoveredToolInfo.java:**

```java
package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;

public record DiscoveredToolInfo(
    String name,
    String description,
    @JsonProperty("input_schema") Object inputSchema
) {}
```

**ToolDiscoverResponse.java:**

```java
package com.persistentagent.api.model.response;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

public record ToolDiscoverResponse(
    @JsonProperty("server_id") String serverId,
    @JsonProperty("server_name") String serverName,
    String status,
    String error,
    List<DiscoveredToolInfo> tools
) {}
```

### Step 4: Create ToolServerNotFoundException

```java
package com.persistentagent.api.exception;

public class ToolServerNotFoundException extends RuntimeException {

    private final String serverId;

    public ToolServerNotFoundException(String serverId) {
        super("Tool server not found: " + serverId);
        this.serverId = serverId;
    }

    public String getServerId() {
        return serverId;
    }
}
```

Add handler in `GlobalExceptionHandler.java` following the `AgentNotFoundException` pattern:

```java
@ExceptionHandler(ToolServerNotFoundException.class)
public ResponseEntity<Map<String, Object>> handleToolServerNotFound(ToolServerNotFoundException ex) {
    return buildErrorResponse(HttpStatus.NOT_FOUND, ex.getMessage());
}
```

### Step 5: Create ToolServerRepository

```java
package com.persistentagent.api.repository;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.*;

@Repository
public class ToolServerRepository {

    private final JdbcTemplate jdbcTemplate;

    public ToolServerRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public Map<String, Object> insert(String tenantId, String name, String url,
                                       String authType, String authToken) {
        return jdbcTemplate.queryForMap(
            """
            INSERT INTO tool_servers (tenant_id, name, url, auth_type, auth_token)
            VALUES (?, ?, ?, ?, ?)
            RETURNING server_id, tenant_id, name, url, auth_type, auth_token, status, created_at, updated_at
            """,
            tenantId, name, url, authType, authToken
        );
    }

    public List<Map<String, Object>> listByTenant(String tenantId, String status, int limit) {
        if (status != null && !status.isBlank()) {
            return jdbcTemplate.queryForList(
                """
                SELECT server_id, tenant_id, name, url, auth_type, status, created_at, updated_at
                FROM tool_servers
                WHERE tenant_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tenantId, status, limit
            );
        }
        return jdbcTemplate.queryForList(
            """
            SELECT server_id, tenant_id, name, url, auth_type, status, created_at, updated_at
            FROM tool_servers
            WHERE tenant_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            tenantId, limit
        );
    }

    public Optional<Map<String, Object>> findById(String tenantId, String serverId) {
        List<Map<String, Object>> rows = jdbcTemplate.queryForList(
            """
            SELECT server_id, tenant_id, name, url, auth_type, auth_token, status, created_at, updated_at
            FROM tool_servers
            WHERE tenant_id = ? AND server_id = ?::uuid
            """,
            tenantId, serverId
        );
        return rows.isEmpty() ? Optional.empty() : Optional.of(rows.get(0));
    }

    public Optional<Map<String, Object>> update(String tenantId, String serverId,
                                                  String name, String url,
                                                  String authType, String authToken,
                                                  String status) {
        List<Map<String, Object>> rows = jdbcTemplate.queryForList(
            """
            UPDATE tool_servers
            SET name = COALESCE(?, name),
                url = COALESCE(?, url),
                auth_type = COALESCE(?, auth_type),
                auth_token = CASE WHEN ? IS NOT NULL THEN ? ELSE auth_token END,
                status = COALESCE(?, status),
                updated_at = NOW()
            WHERE tenant_id = ? AND server_id = ?::uuid
            RETURNING server_id, tenant_id, name, url, auth_type, auth_token, status, created_at, updated_at
            """,
            name, url, authType, authToken, authToken, status, tenantId, serverId
        );
        return rows.isEmpty() ? Optional.empty() : Optional.of(rows.get(0));
    }

    public boolean delete(String tenantId, String serverId) {
        int affected = jdbcTemplate.update(
            "DELETE FROM tool_servers WHERE tenant_id = ? AND server_id = ?::uuid",
            tenantId, serverId
        );
        return affected > 0;
    }

    public List<Map<String, Object>> findByTenantAndNames(String tenantId, List<String> names) {
        if (names == null || names.isEmpty()) {
            return List.of();
        }
        String placeholders = String.join(",", Collections.nCopies(names.size(), "?"));
        List<Object> params = new ArrayList<>();
        params.add(tenantId);
        params.addAll(names);
        return jdbcTemplate.queryForList(
            "SELECT server_id, tenant_id, name, url, auth_type, status FROM tool_servers WHERE tenant_id = ? AND name IN (" + placeholders + ")",
            params.toArray()
        );
    }
}
```

### Step 6: Create ToolServerService

```java
package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.ToolServerNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.ToolServerCreateRequest;
import com.persistentagent.api.model.request.ToolServerUpdateRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.repository.ToolServerRepository;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.OffsetDateTime;
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
```

**Note:** Import `com.persistentagent.api.util.DateTimeUtil` for timestamp conversion — this follows the existing pattern in `AgentService`.

### Step 7: Create ToolServerController

```java
package com.persistentagent.api.controller;

import com.persistentagent.api.model.request.ToolServerCreateRequest;
import com.persistentagent.api.model.request.ToolServerUpdateRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.service.ToolServerService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/v1/tool-servers")
public class ToolServerController {

    private final ToolServerService service;

    public ToolServerController(ToolServerService service) {
        this.service = service;
    }

    @PostMapping
    public ResponseEntity<ToolServerResponse> create(@Valid @RequestBody ToolServerCreateRequest request) {
        ToolServerResponse response = service.createToolServer(request);
        return ResponseEntity.status(HttpStatus.CREATED).body(response);
    }

    @GetMapping
    public ResponseEntity<List<ToolServerSummaryResponse>> list(
            @RequestParam(required = false) String status,
            @RequestParam(required = false) Integer limit) {
        return ResponseEntity.ok(service.listToolServers(status, limit));
    }

    @GetMapping("/{serverId}")
    public ResponseEntity<ToolServerResponse> get(@PathVariable String serverId) {
        return ResponseEntity.ok(service.getToolServer(serverId));
    }

    @PutMapping("/{serverId}")
    public ResponseEntity<ToolServerResponse> update(
            @PathVariable String serverId,
            @Valid @RequestBody ToolServerUpdateRequest request) {
        return ResponseEntity.ok(service.updateToolServer(serverId, request));
    }

    @DeleteMapping("/{serverId}")
    public ResponseEntity<Void> delete(@PathVariable String serverId) {
        service.deleteToolServer(serverId);
        return ResponseEntity.noContent().build();
    }

    @PostMapping("/{serverId}/discover")
    public ResponseEntity<ToolDiscoverResponse> discover(@PathVariable String serverId) {
        return ResponseEntity.ok(service.discoverTools(serverId));
    }
}
```

### Step 8: Write unit tests

**ToolServerControllerTest.java** — Test all endpoints using `@WebMvcTest` + `@MockBean ToolServerService`:

- `testCreateToolServer_success` — POST with valid body returns 201
- `testCreateToolServer_missingName` — POST without name returns 400
- `testCreateToolServer_invalidNamePattern` — POST with uppercase name returns 400
- `testListToolServers_noFilter` — GET returns list
- `testListToolServers_withStatusFilter` — GET with `?status=active` returns filtered list
- `testGetToolServer_found` — GET by ID returns server with masked token
- `testGetToolServer_notFound` — GET by ID returns 404
- `testUpdateToolServer_success` — PUT returns updated server
- `testDeleteToolServer_success` — DELETE returns 204
- `testDeleteToolServer_notFound` — DELETE returns 404
- `testDiscoverTools_reachable` — POST discover returns tool list
- `testDiscoverTools_unreachable` — POST discover returns unreachable status

**ToolServerServiceTest.java** — Test service logic with mocked repository:

- `testCreateToolServer_duplicateName_throwsValidation` — DuplicateKeyException → ValidationException
- `testCreateToolServer_bearerWithoutToken_throwsValidation` — auth_type=bearer_token without token → error
- `testCreateToolServer_invalidUrl_throwsValidation` — non-HTTP URL → error
- `testMaskToken_short` — token ≤ 8 chars → `"****"`
- `testMaskToken_long` — token > 8 chars → `"xxxx...xxxx"` format

## Acceptance Criteria

- [ ] `POST /v1/tool-servers` creates a tool server and returns 201 with full response
- [ ] `GET /v1/tool-servers` lists servers (auth_token omitted), supports `status` filter
- [ ] `GET /v1/tool-servers/{serverId}` returns server detail with masked auth_token
- [ ] `PUT /v1/tool-servers/{serverId}` updates server config, supports partial updates
- [ ] `DELETE /v1/tool-servers/{serverId}` removes server and returns 204
- [ ] `POST /v1/tool-servers/{serverId}/discover` probes MCP server and returns discovered tools
- [ ] Duplicate `(tenant_id, name)` on create returns validation error (not 500)
- [ ] `auth_token` required when `auth_type = 'bearer_token'`
- [ ] `name` validated against `^[a-z0-9][a-z0-9-]*$` pattern
- [ ] `url` validated as HTTP or HTTPS
- [ ] `ToolServerNotFoundException` returns 404 with consistent error format
- [ ] All unit tests pass

## Testing Requirements

- **Unit tests:** Controller tests using `@WebMvcTest` with mocked service. Service tests with mocked repository. Cover all validation paths, CRUD operations, discover success/failure, and token masking.
- **Failure scenarios:** Duplicate name, invalid auth_type, missing auth_token for bearer, invalid URL scheme, server not found on GET/PUT/DELETE, unreachable server on discover.

## Constraints and Guardrails

- Follow the existing Agent CRUD pattern exactly (controller → service → repository with JdbcTemplate).
- Do not add ORM or JPA annotations — use raw JDBC queries via `JdbcTemplate`.
- Do not implement MCP session management in the API service — the discover endpoint uses raw HTTP requests for simplicity.
- Do not persist discovered tools — the discover endpoint is stateless.
- Do not modify the `tool_servers` table schema — use it as-is from Task 1.
- Use `DateTimeUtil.toOffsetDateTime()` for timestamp conversion (existing utility).

## Assumptions

- Task 1 has been completed (`tool_servers` table exists).
- The `GlobalExceptionHandler` already handles `ValidationException` → 400 and can be extended with `ToolServerNotFoundException` → 404.
- The `DateTimeUtil` class in `com.persistentagent.api.util` provides timestamp conversion utilities.
- The discover endpoint's raw HTTP approach (JSON-RPC over HTTP POST) is sufficient for tool discovery. The worker uses the proper MCP SDK for actual tool execution.
- MCP servers using streamable HTTP transport respond to JSON-RPC messages either as plain JSON or SSE (text/event-stream).

<!-- AGENT_TASK_END: task-2-tool-server-api.md -->
