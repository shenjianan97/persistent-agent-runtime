<!-- AGENT_TASK_START: task-6-multipart-and-injection.md -->

# Task 6 — Multipart Task Submission + Input File Injection

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Sections 2 and 4: file input, multipart submission, input file injection)
2. `docs/exec-plans/active/agent-capabilities/track-2/plan.md` — Track 2 execution plan
3. `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` — existing task submission endpoint
4. `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` — existing task submission logic
5. `services/api-service/src/main/java/com/persistentagent/api/model/request/TaskSubmissionRequest.java` — existing task request model
6. `services/worker-service/executor/graph.py` — `execute_task()` entry point (lines 406+)
7. `services/worker-service/storage/s3_client.py` — Track 1 output: S3Client class
8. `services/api-service/src/main/java/com/persistentagent/api/service/S3StorageService.java` — Track 1 output: Java S3 client
9. `services/api-service/src/main/java/com/persistentagent/api/repository/ArtifactRepository.java` — Track 1 output: Java JDBC artifact queries

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-2/progress.md` to "Done".

## Context

This task adds two capabilities:

1. **API side:** A new multipart endpoint for submitting tasks with file attachments. Files are uploaded to S3 and recorded as input artifacts in the `task_artifacts` table.

2. **Worker side:** Input file injection into the sandbox at task start. The worker queries for input artifacts, downloads them from S3, and writes them into the sandbox filesystem before the LLM loop begins.

The existing JSON-only task submission endpoint remains unchanged.

## Task-Specific Shared Contract

- Multipart endpoint: `POST /v1/tasks` with `Content-Type: multipart/form-data`
- Parts: `task_request` (JSON string) + `files` (binary file parts)
- File size limits: 50 MB per file, 200 MB total
- If files are attached, the target agent must have `sandbox.enabled: true` — else 400
- Input files stored in S3 under `{tenant_id}/{task_id}/input/{filename}`
- Input artifacts recorded in `task_artifacts` with `direction='input'`
- Worker reads input artifacts from DB, downloads from S3, writes into sandbox at `/home/user/{filename}`
- Worker adds system message informing the agent about available input files and their paths
- Existing JSON-only endpoint unchanged and fully backward compatible

## Affected Component

- **Service/Module:** API Service (Task Controller) + Worker Service (Executor)
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` (modify)
  - `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` (modify)
  - `services/api-service/src/main/resources/application.yml` (modify)
  - `services/worker-service/executor/graph.py` (modify — input file injection)
  - `services/api-service/src/test/java/com/persistentagent/api/controller/TaskControllerTest.java` (new or modify)
  - `services/worker-service/tests/test_executor.py` (modify — injection tests)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 1 (DB migration), Task 2 (Sandbox Provisioner), Track 1 Task 3 (S3Client), Track 1 Task 4 (API S3StorageService + ArtifactRepository)
- **Provides output to:** Task 9 (Integration Tests)
- **Shared interfaces/contracts:** Track 1's S3StorageService, ArtifactRepository; sandbox provisioner

## Implementation Specification

### Step 1: Configure multipart size limits

Modify `services/api-service/src/main/resources/application.yml` to add multipart settings under the existing `spring` key:

```yaml
spring:
  servlet:
    multipart:
      max-file-size: 50MB
      max-request-size: 200MB
```

### Step 2: Update existing JSON endpoint and add multipart endpoint to TaskController

Modify `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java`. First, update the existing JSON-only `@PostMapping` to explicitly set `consumes = MediaType.APPLICATION_JSON_VALUE`. This prevents routing ambiguity when adding the multipart endpoint — Spring needs explicit `consumes` on both mappings to cleanly route based on `Content-Type`:

```java
    // Update the existing submitTask method annotation:
    @PostMapping(consumes = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<TaskSubmissionResponse> submitTask(/* existing params */) {
        // ... existing implementation unchanged ...
    }
```

Then add the multipart submission method:

```java
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.multipart.MultipartFile;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.validation.Validator;
import jakarta.validation.ConstraintViolation;

import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;
```

Add new fields and constructor parameters:

```java
    private final ObjectMapper objectMapper;
    private final Validator validator;

    public TaskController(TaskService taskService, TaskEventService taskEventService,
                          ObjectMapper objectMapper, Validator validator) {
        this.taskService = taskService;
        this.taskEventService = taskEventService;
        this.objectMapper = objectMapper;
        this.validator = validator;
    }
```

Add the multipart endpoint. Note: since the `task_request` JSON part is deserialized manually (not via `@Valid @RequestBody`), we must programmatically run Bean Validation to enforce the same constraints as the JSON endpoint:

```java
    @PostMapping(consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<TaskSubmissionResponse> submitTaskMultipart(
            @RequestPart("task_request") String taskRequestJson,
            @RequestPart(value = "files", required = false) List<MultipartFile> files) {
        try {
            TaskSubmissionRequest request = objectMapper.readValue(taskRequestJson, TaskSubmissionRequest.class);

            // Programmatically validate — the JSON endpoint gets this for free via @Valid,
            // but manual deserialization skips it. This ensures identical validation behavior.
            Set<ConstraintViolation<TaskSubmissionRequest>> violations = validator.validate(request);
            if (!violations.isEmpty()) {
                String errors = violations.stream()
                        .map(v -> v.getPropertyPath() + ": " + v.getMessage())
                        .collect(Collectors.joining(", "));
                throw new com.persistentagent.api.exception.ValidationException(errors);
            }

            TaskSubmissionResponse response = taskService.submitTaskWithFiles(request, files);
            return ResponseEntity.status(HttpStatus.CREATED).body(response);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new com.persistentagent.api.exception.ValidationException(
                    "Invalid task_request JSON: " + e.getMessage());
        }
    }
```

### Step 3: Add submitTaskWithFiles to TaskService

Modify `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` to add the file-handling method:

```java
import org.springframework.web.multipart.MultipartFile;
import com.persistentagent.api.repository.ArtifactRepository;

import java.io.IOException;
```

Add the ArtifactRepository and S3StorageService as constructor dependencies:

```java
    private final ArtifactRepository artifactRepository;
    private final S3StorageService s3StorageService;
```

Add the new method:

```java
    @Transactional
    public TaskSubmissionResponse submitTaskWithFiles(
            TaskSubmissionRequest request, List<MultipartFile> files) {
        // First, submit the task normally (creates the task row)
        TaskSubmissionResponse response = submitTask(request);
        UUID taskId = response.taskId();
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        // If files are present, validate sandbox requirement and upload
        if (files != null && !files.isEmpty()) {
            // Look up the agent to check sandbox config
            Map<String, Object> agentRow = agentRepository.findByIdAndTenant(
                    tenantId, request.agentId())
                    .orElseThrow(() -> new ValidationException(
                            "Agent not found: " + request.agentId()));

            String agentConfigJson = (String) agentRow.get("agent_config");
            boolean sandboxEnabled = isSandboxEnabled(agentConfigJson);

            if (!sandboxEnabled) {
                throw new ValidationException(
                        "File attachments require an agent with sandbox enabled. "
                        + "Agent '" + request.agentId() + "' does not have sandbox.enabled: true.");
            }

            // Upload each file to S3 and record as input artifact.
            // Track uploaded S3 keys so we can clean up orphans if a later step fails
            // (DB rolls back on exception but S3 writes do not).
            List<String> uploadedS3Keys = new java.util.ArrayList<>();
            try {
                for (MultipartFile file : files) {
                    String filename = file.getOriginalFilename();
                    if (filename == null || filename.isBlank()) {
                        filename = "unnamed_file";
                    }

                    String s3Key = tenantId + "/" + taskId + "/input/" + filename;
                    String contentType = file.getContentType();
                    if (contentType == null || contentType.isBlank()) {
                        contentType = "application/octet-stream";
                    }

                    try {
                        byte[] data = file.getBytes();
                        s3StorageService.upload(s3Key, data, contentType);
                        uploadedS3Keys.add(s3Key);
                        artifactRepository.insert(
                                taskId, tenantId, filename, "input",
                                contentType, data.length, s3Key);
                    } catch (IOException e) {
                        throw new RuntimeException(
                                "Failed to read uploaded file: " + filename, e);
                    }
                }
            } catch (Exception e) {
                // Best-effort cleanup: delete any S3 objects already uploaded.
                // The DB transaction will roll back automatically, but S3 is not transactional.
                for (String orphanKey : uploadedS3Keys) {
                    try {
                        s3StorageService.delete(orphanKey);
                    } catch (Exception cleanupEx) {
                        // Log but don't mask the original exception
                    }
                }
                throw e;
            }
        }

        return response;
    }

    private boolean isSandboxEnabled(String agentConfigJson) {
        try {
            var configNode = objectMapper.readTree(agentConfigJson);
            var sandboxNode = configNode.get("sandbox");
            if (sandboxNode == null || sandboxNode.isNull()) {
                return false;
            }
            var enabledNode = sandboxNode.get("enabled");
            return enabledNode != null && enabledNode.asBoolean(false);
        } catch (Exception e) {
            return false;
        }
    }
```

### Step 4: Add input file injection to worker execute_task()

Modify `services/worker-service/executor/graph.py` — add an input file injection method and call it after sandbox provisioning, before the LLM loop.

Add the following method to `GraphExecutor`:

```python
    async def _inject_input_files(self, sandbox, task_id: str, tenant_id: str) -> list[str]:
        """Download input artifacts from S3 and write them into the sandbox.

        Args:
            sandbox: E2B Sandbox instance
            task_id: UUID string
            tenant_id: tenant ID

        Returns:
            List of injected filenames (for system message generation)
        """
        # Query task_artifacts for input files
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT filename, s3_key, content_type, size_bytes
                   FROM task_artifacts
                   WHERE task_id = $1::uuid AND direction = 'input'
                   ORDER BY created_at""",
                task_id,
            )

        if not rows:
            return []

        injected_files = []
        for row in rows:
            filename = row["filename"]
            s3_key = row["s3_key"]
            size_bytes = row["size_bytes"]

            try:
                # Download from S3 via Track 1's S3Client (already async)
                data = await self.s3_client.download(s3_key)

                # Write into sandbox filesystem
                sandbox_path = f"/home/user/{filename}"
                await asyncio.to_thread(sandbox.files.write, sandbox_path, data)

                injected_files.append(filename)

                logger.info(
                    "input_file_injected",
                    extra={
                        "task_id": task_id,
                        "filename": filename,
                        "sandbox_path": sandbox_path,
                        "size_bytes": size_bytes,
                    },
                )

            except Exception as e:
                logger.error(
                    "input_file_injection_failed",
                    extra={
                        "task_id": task_id,
                        "filename": filename,
                        "s3_key": s3_key,
                        "error": str(e),
                    },
                )
                raise RuntimeError(
                    f"Failed to inject input file '{filename}' into sandbox: {str(e)}"
                ) from e

        logger.info(
            "input_files_injection_completed",
            extra={
                "task_id": task_id,
                "file_count": len(injected_files),
                "filenames": injected_files,
            },
        )

        return injected_files
```

### Step 5: Build system message for injected files

Add a helper method to `GraphExecutor` that generates a system message about available input files:

```python
    def _build_input_files_system_message(self, filenames: list[str]) -> str:
        """Build a system message telling the agent about available input files.

        Args:
            filenames: List of filenames injected into the sandbox

        Returns:
            System message string
        """
        if not filenames:
            return ""

        file_list = "\n".join(f"  - /home/user/{f}" for f in filenames)
        return (
            f"The following input files have been provided and are available "
            f"in the sandbox filesystem:\n{file_list}\n\n"
            f"You can read these files using sandbox_read_file or process them "
            f"with sandbox_exec commands."
        )
```

### Step 6: Integrate injection into execute_task()

The injection call should be placed in `execute_task()` after sandbox provisioning and before the LLM loop. This integration will be done as part of Task 7 (Crash Recovery) which handles the full sandbox provisioning flow in `execute_task()`. However, the injection methods must be ready and tested now.

**Note for Task 7:** After provisioning or reconnecting the sandbox, call:
```python
injected_files = await self._inject_input_files(sandbox, task_id, tenant_id)
if injected_files:
    input_files_msg = self._build_input_files_system_message(injected_files)
    # Append to the system prompt or inject as an additional system message
```

### Step 7: Write API unit tests

Create or modify `services/api-service/src/test/java/com/persistentagent/api/controller/TaskMultipartTest.java`:

```java
package com.persistentagent.api.controller;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.model.request.TaskSubmissionRequest;
import com.persistentagent.api.service.TaskService;
import com.persistentagent.api.service.TaskEventService;
import com.persistentagent.api.model.response.TaskSubmissionResponse;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.mock.bean.MockBean;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.web.servlet.MockMvc;

import java.util.UUID;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@WebMvcTest(TaskController.class)
class TaskMultipartTest {

    @Autowired
    private MockMvc mockMvc;

    @MockBean
    private TaskService taskService;

    @MockBean
    private TaskEventService taskEventService;

    @Autowired
    private ObjectMapper objectMapper;

    @Test
    void submitMultipart_withValidRequest_returns201() throws Exception {
        UUID taskId = UUID.randomUUID();
        when(taskService.submitTaskWithFiles(any(), any()))
                .thenReturn(new TaskSubmissionResponse(taskId, "queued"));

        String taskJson = """
                {"agent_id": "agent-1", "input": "Process this file"}
                """;

        MockMultipartFile taskPart = new MockMultipartFile(
                "task_request", "", "application/json", taskJson.getBytes());
        MockMultipartFile filePart = new MockMultipartFile(
                "files", "document.pdf", "application/pdf", "fake pdf content".getBytes());

        mockMvc.perform(multipart("/v1/tasks")
                        .file(taskPart)
                        .file(filePart))
                .andExpect(status().isCreated());
    }

    @Test
    void submitMultipart_withoutFiles_returns201() throws Exception {
        UUID taskId = UUID.randomUUID();
        when(taskService.submitTaskWithFiles(any(), any()))
                .thenReturn(new TaskSubmissionResponse(taskId, "queued"));

        String taskJson = """
                {"agent_id": "agent-1", "input": "No files here"}
                """;

        MockMultipartFile taskPart = new MockMultipartFile(
                "task_request", "", "application/json", taskJson.getBytes());

        mockMvc.perform(multipart("/v1/tasks")
                        .file(taskPart))
                .andExpect(status().isCreated());
    }
}
```

### Step 8: Write worker injection unit tests

Add to `services/worker-service/tests/test_executor.py`:

```python
class TestInputFileInjection:
    @pytest.mark.asyncio
    async def test_inject_no_input_files(self):
        """No input artifacts → returns empty list, no sandbox writes."""
        executor = build_test_executor()
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        executor.pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        result = await executor._inject_input_files(mock_sandbox, "task-123", "default")
        assert result == []

    @pytest.mark.asyncio
    async def test_inject_input_files_writes_to_sandbox(self):
        """Input artifacts are downloaded from S3 and written to sandbox."""
        executor = build_test_executor()
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        rows = [
            {"filename": "data.csv", "s3_key": "default/task-123/input/data.csv",
             "content_type": "text/csv", "size_bytes": 100},
        ]
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=rows)
        executor.pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        executor.s3_client = MagicMock()
        executor.s3_client.download = AsyncMock(return_value=b"csv,data")

        with patch("executor.graph.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = None  # sandbox.files.write (sync E2B SDK call)
            result = await executor._inject_input_files(mock_sandbox, "task-123", "default")

        assert result == ["data.csv"]

    def test_build_input_files_system_message_empty(self):
        executor = build_test_executor()
        msg = executor._build_input_files_system_message([])
        assert msg == ""

    def test_build_input_files_system_message_with_files(self):
        executor = build_test_executor()
        msg = executor._build_input_files_system_message(["data.csv", "readme.txt"])
        assert "/home/user/data.csv" in msg
        assert "/home/user/readme.txt" in msg
        assert "sandbox_read_file" in msg
```

## Acceptance Criteria

- [ ] `application.yml` has `spring.servlet.multipart.max-file-size=50MB` and `max-request-size=200MB`
- [ ] `TaskController` has `submitTaskMultipart()` accepting `@RequestPart("task_request")` and `@RequestPart("files")`
- [ ] Multipart endpoint parses task_request JSON and files list
- [ ] `TaskService.submitTaskWithFiles()` validates sandbox enabled when files are present
- [ ] Files rejected with 400 if agent does not have `sandbox.enabled: true`
- [ ] Each file uploaded to S3 under `{tenant_id}/{task_id}/input/{filename}`
- [ ] Each file recorded in `task_artifacts` with `direction='input'`
- [ ] Existing JSON-only `POST /v1/tasks` endpoint updated with explicit `consumes = MediaType.APPLICATION_JSON_VALUE` for clean routing
- [ ] Existing JSON-only endpoint remains fully functional
- [ ] `GraphExecutor._inject_input_files()` queries task_artifacts, downloads from S3, writes to sandbox
- [ ] Files written to sandbox at `/home/user/{filename}`
- [ ] `_build_input_files_system_message()` generates message listing available files
- [ ] Injection failure raises RuntimeError (task should dead-letter)
- [ ] All unit tests pass for multipart controller and injection logic
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests (API):** Multipart submit with files returns 201. Multipart submit without files returns 201. Invalid JSON in task_request part returns 400.
- **Unit tests (Worker):** No input files returns empty list. Input files downloaded and written to sandbox. System message generation for empty and non-empty file lists.
- **Regression:** `make test` — all existing tests pass.

## Constraints and Guardrails

- Do not modify the existing JSON-only `POST /v1/tasks` endpoint — it must remain fully backward compatible.
- Do not change how tasks are submitted to the queue — files are handled at the API level, not in the queue.
- Do not implement input file injection into `execute_task()` — that integration happens in Task 7. Only provide the helper methods and tests.
- Use Track 1's `S3StorageService` and `ArtifactRepository` on the Java side — do not create separate S3 code.
- Use Track 1's `S3Client` on the Python side for downloading input files.
- File validation (size limits) is handled by Spring Boot multipart configuration, not custom code.

## Assumptions

- Track 1 Tasks 3-4 have been completed (`S3StorageService` and `ArtifactRepository` exist on the Java side).
- Track 1 Task 3 has been completed (`S3Client` with `download()` method exists on the Python side).
- Task 1 has been completed (sandbox config is validated and persisted in agent_config).
- `TaskSubmissionResponse` record has `taskId()` accessor.
- `ArtifactRepository.insert()` method accepts `(taskId, tenantId, filename, direction, contentType, sizeBytes, s3Key)`.
- `S3StorageService.upload(key, data, contentType)` uploads bytes to S3.
- `S3Client.download(key)` is an async method that returns bytes from S3 (internally uses `asyncio.to_thread()` — do NOT wrap with `asyncio.to_thread()` again).
- Spring Boot 3.4 handles `@PostMapping(consumes = MediaType.MULTIPART_FORM_DATA_VALUE)` for multipart content type routing.

<!-- AGENT_TASK_END: task-6-multipart-and-injection.md -->
