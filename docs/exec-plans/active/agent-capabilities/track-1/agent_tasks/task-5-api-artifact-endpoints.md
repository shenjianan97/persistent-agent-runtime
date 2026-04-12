<!-- AGENT_TASK_START: task-5-api-artifact-endpoints.md -->

# Task 5 — API Artifact Endpoints: List and Download

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 2: Artifact Storage — API subsection, Section 4: API Service Changes)
2. `docs/exec-plans/active/agent-capabilities/track-1/plan.md` — Track 1 execution plan
3. `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` — existing controller pattern
4. `services/api-service/src/main/java/com/persistentagent/api/repository/ArtifactRepository.java` — Task 4 output: repository with `findByTaskId()`, `findByTaskIdAndFilename()`
5. `services/api-service/src/main/java/com/persistentagent/api/service/S3StorageService.java` — Task 4 output: S3 download service
6. `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` — task lookup for tenant validation

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-1/progress.md` to "Done".

## Context

Track 1 provides REST endpoints for listing and downloading artifacts associated with a task. These endpoints are consumed by both the Console (Task 7) and external API clients.

The list endpoint returns metadata for all artifacts of a task. The download endpoint streams the artifact file from S3 with the correct `Content-Type` and `Content-Disposition` headers.

## Task-Specific Shared Contract

- List endpoint: `GET /v1/tasks/{taskId}/artifacts` — returns `List<ArtifactMetadata>`, optional `direction` query param
- Download endpoint: `GET /v1/tasks/{taskId}/artifacts/{filename}` — streams file from S3, optional `direction` query param (defaults to `output`)
- Tenant validation: the task must belong to the requesting tenant (checked via `TaskRepository.findByIdAndTenant()`)
- Tenant ID is resolved internally by `ArtifactService` using `ValidationConstants.DEFAULT_TENANT_ID` (same pattern as `TaskService`)
- Download sets `Content-Type` from artifact metadata and `Content-Disposition: attachment; filename="..."` for browser download

## Affected Component

- **Service/Module:** API Service — Controller and Service
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/controller/ArtifactController.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/service/ArtifactService.java` (new)
  - `services/api-service/src/test/java/com/persistentagent/api/controller/ArtifactControllerTest.java` (new)
  - `services/api-service/src/test/java/com/persistentagent/api/service/ArtifactServiceTest.java` (new)
- **Change type:** new code

## Dependencies

- **Must complete first:** Task 4 (API Artifact Repository + S3 — provides `ArtifactRepository` and `S3StorageService`)
- **Provides output to:** Task 7 (Console Artifacts Tab — calls these endpoints), Task 8 (Integration Tests)
- **Shared interfaces/contracts:** REST API contract consumed by Console and external clients

## Implementation Specification

### Step 1: Create ArtifactService

Create `services/api-service/src/main/java/com/persistentagent/api/service/ArtifactService.java`:

```java
package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.model.ArtifactMetadata;
import com.persistentagent.api.repository.ArtifactRepository;
import com.persistentagent.api.repository.ArtifactRepository.ArtifactWithS3Key;
import com.persistentagent.api.repository.TaskRepository;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;
import software.amazon.awssdk.core.ResponseInputStream;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

@Service
public class ArtifactService {

    private final ArtifactRepository artifactRepository;
    private final TaskRepository taskRepository;
    private final S3StorageService s3StorageService;

    public ArtifactService(ArtifactRepository artifactRepository,
                           TaskRepository taskRepository,
                           S3StorageService s3StorageService) {
        this.artifactRepository = artifactRepository;
        this.taskRepository = taskRepository;
        this.s3StorageService = s3StorageService;
    }

    /**
     * Lists all artifacts for a task, optionally filtered by direction.
     * Validates that the task belongs to the tenant.
     */
    public List<ArtifactMetadata> listArtifacts(UUID taskId, String direction) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        // Validate task belongs to tenant
        taskRepository.findByIdAndTenant(taskId, tenantId)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND,
                        "Task not found: " + taskId));

        return artifactRepository.findByTaskId(taskId, tenantId, direction);
    }

    /**
     * Downloads an artifact file from S3.
     * Returns the artifact metadata and S3 response stream.
     */
    public ArtifactDownload downloadArtifact(UUID taskId,
                                              String filename, String direction) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        // Validate task belongs to tenant
        taskRepository.findByIdAndTenant(taskId, tenantId)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND,
                        "Task not found: " + taskId));

        // Look up artifact
        ArtifactWithS3Key artifact = artifactRepository
                .findByTaskIdAndFilename(taskId, tenantId, filename, direction)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND,
                        "Artifact not found: " + filename));

        // Download from S3
        ResponseInputStream<GetObjectResponse> stream = s3StorageService
                .download(artifact.s3Key())
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND,
                        "Artifact file not found in storage: " + filename));

        return new ArtifactDownload(artifact.metadata(), stream);
    }

    /**
     * Download result containing metadata and the S3 response stream.
     */
    public record ArtifactDownload(
        ArtifactMetadata metadata,
        ResponseInputStream<GetObjectResponse> stream
    ) {}
}
```

### Step 2: Create ArtifactController

Create `services/api-service/src/main/java/com/persistentagent/api/controller/ArtifactController.java`:

```java
package com.persistentagent.api.controller;

import com.persistentagent.api.model.ArtifactMetadata;
import com.persistentagent.api.service.ArtifactService;
import com.persistentagent.api.service.ArtifactService.ArtifactDownload;
import org.springframework.core.io.InputStreamResource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.UUID;

@RestController
@RequestMapping("/v1/tasks/{taskId}/artifacts")
public class ArtifactController {

    private final ArtifactService artifactService;

    public ArtifactController(ArtifactService artifactService) {
        this.artifactService = artifactService;
    }

    /**
     * List all artifacts for a task.
     * Optional query parameter: direction (input/output)
     */
    @GetMapping
    public ResponseEntity<List<ArtifactMetadata>> listArtifacts(
            @PathVariable UUID taskId,
            @RequestParam(name = "direction", required = false) String direction) {
        List<ArtifactMetadata> artifacts = artifactService.listArtifacts(taskId, direction);
        return ResponseEntity.ok(artifacts);
    }

    /**
     * Download a specific artifact by filename.
     * Optional query parameter: direction (defaults to "output")
     * Streams the file from S3 with correct Content-Type and Content-Disposition headers.
     */
    @GetMapping("/{filename}")
    public ResponseEntity<InputStreamResource> downloadArtifact(
            @PathVariable UUID taskId,
            @PathVariable String filename,
            @RequestParam(name = "direction", defaultValue = "output") String direction) {
        ArtifactDownload download = artifactService.downloadArtifact(
                taskId, filename, direction);

        ArtifactMetadata metadata = download.metadata();
        InputStreamResource resource = new InputStreamResource(download.stream());

        return ResponseEntity.ok()
                .contentType(MediaType.parseMediaType(metadata.contentType()))
                .contentLength(metadata.sizeBytes())
                .header(HttpHeaders.CONTENT_DISPOSITION,
                        "attachment; filename=\"" + metadata.filename() + "\"")
                .body(resource);
    }
}
```

### Step 3: Create ArtifactController unit tests

Create `services/api-service/src/test/java/com/persistentagent/api/controller/ArtifactControllerTest.java`:

```java
package com.persistentagent.api.controller;

import com.persistentagent.api.model.ArtifactMetadata;
import com.persistentagent.api.service.ArtifactService;
import com.persistentagent.api.service.ArtifactService.ArtifactDownload;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.mock.bean.MockBean;
import org.springframework.http.HttpStatus;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.server.ResponseStatusException;
import software.amazon.awssdk.core.ResponseInputStream;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;

import java.io.ByteArrayInputStream;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.UUID;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(ArtifactController.class)
class ArtifactControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockBean
    private ArtifactService artifactService;

    @Test
    void listArtifacts_returnsArtifactList() throws Exception {
        UUID taskId = UUID.randomUUID();
        ArtifactMetadata artifact = new ArtifactMetadata(
                UUID.randomUUID(), taskId, "report.pdf", "output",
                "application/pdf", 1024L, OffsetDateTime.now());

        when(artifactService.listArtifacts(eq(taskId), isNull()))
                .thenReturn(List.of(artifact));

        mockMvc.perform(get("/v1/tasks/{taskId}/artifacts", taskId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].filename").value("report.pdf"))
                .andExpect(jsonPath("$[0].direction").value("output"))
                .andExpect(jsonPath("$[0].contentType").value("application/pdf"))
                .andExpect(jsonPath("$[0].sizeBytes").value(1024));
    }

    @Test
    void listArtifacts_withDirectionFilter() throws Exception {
        UUID taskId = UUID.randomUUID();

        when(artifactService.listArtifacts(eq(taskId), eq("output")))
                .thenReturn(List.of());

        mockMvc.perform(get("/v1/tasks/{taskId}/artifacts", taskId)
                        .param("direction", "output"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$").isArray())
                .andExpect(jsonPath("$").isEmpty());
    }

    @Test
    void listArtifacts_taskNotFound_returns404() throws Exception {
        UUID taskId = UUID.randomUUID();

        when(artifactService.listArtifacts(eq(taskId), isNull()))
                .thenThrow(new ResponseStatusException(HttpStatus.NOT_FOUND, "Task not found"));

        mockMvc.perform(get("/v1/tasks/{taskId}/artifacts", taskId))
                .andExpect(status().isNotFound());
    }

    @Test
    void downloadArtifact_streamsFileWithCorrectHeaders() throws Exception {
        UUID taskId = UUID.randomUUID();
        byte[] content = "file content here".getBytes();
        ArtifactMetadata metadata = new ArtifactMetadata(
                UUID.randomUUID(), taskId, "report.pdf", "output",
                "application/pdf", content.length, OffsetDateTime.now());

        GetObjectResponse getObjectResponse = GetObjectResponse.builder()
                .contentType("application/pdf")
                .contentLength((long) content.length)
                .build();
        ResponseInputStream<GetObjectResponse> stream = new ResponseInputStream<>(
                getObjectResponse, new ByteArrayInputStream(content));

        ArtifactDownload download = new ArtifactDownload(metadata, stream);

        when(artifactService.downloadArtifact(eq(taskId), eq("report.pdf"), eq("output")))
                .thenReturn(download);

        mockMvc.perform(get("/v1/tasks/{taskId}/artifacts/{filename}", taskId, "report.pdf"))
                .andExpect(status().isOk())
                .andExpect(header().string("Content-Type", "application/pdf"))
                .andExpect(header().string("Content-Disposition", "attachment; filename=\"report.pdf\""))
                .andExpect(content().bytes(content));
    }

    @Test
    void downloadArtifact_artifactNotFound_returns404() throws Exception {
        UUID taskId = UUID.randomUUID();

        when(artifactService.downloadArtifact(eq(taskId), eq("missing.pdf"), eq("output")))
                .thenThrow(new ResponseStatusException(HttpStatus.NOT_FOUND, "Artifact not found"));

        mockMvc.perform(get("/v1/tasks/{taskId}/artifacts/{filename}", taskId, "missing.pdf"))
                .andExpect(status().isNotFound());
    }
}
```

### Step 4: Create ArtifactService unit tests

Create `services/api-service/src/test/java/com/persistentagent/api/service/ArtifactServiceTest.java`:

```java
package com.persistentagent.api.service;

import com.persistentagent.api.model.ArtifactMetadata;
import com.persistentagent.api.repository.ArtifactRepository;
import com.persistentagent.api.repository.ArtifactRepository.ArtifactWithS3Key;
import com.persistentagent.api.repository.TaskRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.web.server.ResponseStatusException;
import software.amazon.awssdk.core.ResponseInputStream;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;

import java.io.ByteArrayInputStream;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class ArtifactServiceTest {

    @Mock
    private ArtifactRepository artifactRepository;

    @Mock
    private TaskRepository taskRepository;

    @Mock
    private S3StorageService s3StorageService;

    private ArtifactService artifactService;

    @BeforeEach
    void setUp() {
        artifactService = new ArtifactService(artifactRepository, taskRepository, s3StorageService);
    }

    @Test
    void listArtifacts_taskNotFound_throws404() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default")).thenReturn(Optional.empty());

        assertThrows(ResponseStatusException.class, () ->
                artifactService.listArtifacts(taskId, null));
    }

    @Test
    void listArtifacts_returnsArtifactsFromRepository() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default"))
                .thenReturn(Optional.of(Map.of("task_id", taskId)));

        ArtifactMetadata artifact = new ArtifactMetadata(
                UUID.randomUUID(), taskId, "report.pdf", "output",
                "application/pdf", 1024L, OffsetDateTime.now());
        when(artifactRepository.findByTaskId(taskId, "default", null))
                .thenReturn(List.of(artifact));

        List<ArtifactMetadata> result = artifactService.listArtifacts(taskId, null);
        assertEquals(1, result.size());
        assertEquals("report.pdf", result.get(0).filename());
    }

    @Test
    void downloadArtifact_taskNotFound_throws404() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default")).thenReturn(Optional.empty());

        assertThrows(ResponseStatusException.class, () ->
                artifactService.downloadArtifact(taskId, "report.pdf", "output"));
    }

    @Test
    void downloadArtifact_artifactNotFound_throws404() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default"))
                .thenReturn(Optional.of(Map.of("task_id", taskId)));
        when(artifactRepository.findByTaskIdAndFilename(taskId, "default", "report.pdf", "output"))
                .thenReturn(Optional.empty());

        assertThrows(ResponseStatusException.class, () ->
                artifactService.downloadArtifact(taskId, "report.pdf", "output"));
    }

    @Test
    void downloadArtifact_s3KeyNotFound_throws404() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default"))
                .thenReturn(Optional.of(Map.of("task_id", taskId)));

        ArtifactMetadata metadata = new ArtifactMetadata(
                UUID.randomUUID(), taskId, "report.pdf", "output",
                "application/pdf", 1024L, OffsetDateTime.now());
        when(artifactRepository.findByTaskIdAndFilename(taskId, "default", "report.pdf", "output"))
                .thenReturn(Optional.of(new ArtifactWithS3Key(metadata, "default/task-1/output/report.pdf")));
        when(s3StorageService.download("default/task-1/output/report.pdf"))
                .thenReturn(Optional.empty());

        assertThrows(ResponseStatusException.class, () ->
                artifactService.downloadArtifact(taskId, "report.pdf", "output"));
    }

    @Test
    void downloadArtifact_success_returnsDownloadResult() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default"))
                .thenReturn(Optional.of(Map.of("task_id", taskId)));

        ArtifactMetadata metadata = new ArtifactMetadata(
                UUID.randomUUID(), taskId, "report.pdf", "output",
                "application/pdf", 1024L, OffsetDateTime.now());
        when(artifactRepository.findByTaskIdAndFilename(taskId, "default", "report.pdf", "output"))
                .thenReturn(Optional.of(new ArtifactWithS3Key(metadata, "default/task-1/output/report.pdf")));

        GetObjectResponse getObjectResponse = GetObjectResponse.builder().build();
        ResponseInputStream<GetObjectResponse> stream = new ResponseInputStream<>(
                getObjectResponse, new ByteArrayInputStream(new byte[0]));
        when(s3StorageService.download("default/task-1/output/report.pdf"))
                .thenReturn(Optional.of(stream));

        ArtifactService.ArtifactDownload result =
                artifactService.downloadArtifact(taskId, "report.pdf", "output");

        assertNotNull(result);
        assertEquals("report.pdf", result.metadata().filename());
        assertNotNull(result.stream());
    }
}
```

### Step 5: Add output artifacts to task status response

The design doc Section 2 requires `GET /v1/tasks/{id}` to include output artifacts in the response. Modify `TaskService.getTaskStatus()` to query for output artifacts and include them in the response.

In `TaskService.java`, inject `ArtifactRepository` and add artifact querying to `getTaskStatus()`:

```java
// Add to TaskService constructor parameters:
private final ArtifactRepository artifactRepository;

// In getTaskStatus(), after building the existing response fields:
String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
List<ArtifactMetadata> artifacts = artifactRepository.findByTaskId(taskId, tenantId, "output");
```

Include the `artifacts` list in the `TaskStatusResponse`. If no output artifacts exist, the list will be empty.

**Affected files:**
- `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` (modify — inject `ArtifactRepository`, query artifacts in `getTaskStatus()`)
- `services/api-service/src/main/java/com/persistentagent/api/model/response/TaskStatusResponse.java` (modify — add `List<ArtifactMetadata> artifacts` field)

**Note:** This step modifies `TaskService.java` — the only exception to the "do not modify TaskController" constraint. The controller is not modified; only the service layer adds the artifact query.

## Acceptance Criteria

- [ ] `ArtifactController.java` exists with `@RequestMapping("/v1/tasks/{taskId}/artifacts")`
- [ ] `GET /v1/tasks/{taskId}/artifacts` returns `List<ArtifactMetadata>` with optional `direction` query param
- [ ] `GET /v1/tasks/{taskId}/artifacts/{filename}` streams artifact file from S3 with optional `direction` query param (default: `output`)
- [ ] Download endpoint sets `Content-Type` from artifact metadata
- [ ] Download endpoint sets `Content-Disposition: attachment; filename="..."` header
- [ ] Download endpoint sets `Content-Length` header
- [ ] Task ownership validated via `TaskRepository.findByIdAndTenant()` — returns 404 if task not found for tenant
- [ ] Artifact not found returns 404
- [ ] S3 file not found returns 404
- [ ] `ArtifactService.java` exists and orchestrates repository + S3 calls
- [ ] `GET /v1/tasks/{id}` response includes an `artifacts` array with output artifacts when they exist
- [ ] `TaskService.getTaskStatus()` queries `ArtifactRepository.findByTaskId()` for output artifacts
- [ ] All unit tests pass (controller + service)
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests (Controller):** Test list endpoint returns artifacts. Test list with direction filter. Test task not found returns 404. Test download streams file with correct headers. Test artifact not found returns 404.
- **Unit tests (Service):** Test task not found throws 404. Test list delegates to repository. Test download with missing task/artifact/S3 key throws 404. Test successful download returns metadata + stream.
- **Integration tests:** Full endpoint testing covered by Task 8.
- **Regression tests:** Run `make test` — all existing API tests must still pass.

## Constraints and Guardrails

- Do not add file upload endpoints — the API only serves list and download. Upload is done by the worker (Task 6).
- Do not add multipart support to task submission — that is Track 2.
- Do not modify `TaskController.java` — artifact endpoints are in a separate controller.
- Use `InputStreamResource` for streaming — do not buffer the entire file in memory.
- Follow existing patterns: constructor injection, `@RestController`, `ResponseEntity<...>`.
- Tenant ID is resolved internally by `ArtifactService` using `ValidationConstants.DEFAULT_TENANT_ID` (same pattern as `TaskService`). Do not accept tenant ID from request headers.

## Assumptions

- Task 4 has been completed (`ArtifactRepository`, `ArtifactMetadata`, `S3StorageService` exist).
- The `TaskRepository.findByIdAndTenant()` method is available for tenant validation.
- Spring Boot automatically serializes the `ArtifactMetadata` record to JSON (Jackson support for records is built into Spring Boot 3.x).
- The `@WebMvcTest` annotation in controller tests auto-configures MockMvc without starting a full server.
- `@MockBean` from `spring-boot-test` is used for mocking service dependencies in controller tests.

<!-- AGENT_TASK_END: task-5-api-artifact-endpoints.md -->
