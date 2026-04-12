<!-- AGENT_TASK_START: task-8-integration-tests.md -->

# Task 8 — Integration Tests: Output Artifact Flow

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (full document for end-to-end understanding)
2. `docs/exec-plans/active/agent-capabilities/track-1/plan.md` — Track 1 execution plan
3. `tests/backend-integration/conftest.py` — existing E2E test infrastructure and fixtures
4. `tests/backend-integration/helpers/api_client.py` — `ApiClient` class for API calls
5. `tests/backend-integration/helpers/e2e_context.py` — `E2EContext` class for test orchestration
6. `tests/backend-integration/test_happy_path.py` — existing integration test patterns
7. `tests/backend-integration/helpers/mock_llm.py` — mock LLM behavior helpers
8. `services/worker-service/tools/upload_artifact.py` — Task 6 output: `upload_artifact` tool implementation

**CRITICAL POST-WORK:** After completing this task:
1. Run the integration tests with `make e2e-test` and verify all new tests pass.
2. Run the full test suite with `make test` and verify no regressions.
3. Update the status in `docs/exec-plans/active/agent-capabilities/track-1/progress.md` to "Done".

## Context

Track 1 integration tests validate the end-to-end output artifact flow: an agent produces artifacts via the `upload_artifact` tool, and users can list and download them via the API.

These tests use the existing E2E infrastructure (isolated PostgreSQL on port 55433, API on port 8081) and add LocalStack for S3. They follow the same patterns as existing integration tests in `tests/backend-integration/`.

## Task-Specific Shared Contract

- Test agent config includes `upload_artifact` in `allowed_tools`
- Mock LLM produces tool calls to `upload_artifact` with filename, content, and content_type
- After task completion, artifact list endpoint returns the uploaded artifacts
- Download endpoint returns the original content with correct Content-Type
- Tests must clean up artifacts after each test (or rely on test isolation)

## Affected Component

- **Service/Module:** Integration Tests
- **File paths:**
  - `tests/backend-integration/test_artifacts.py` (new — artifact integration tests)
  - `tests/backend-integration/helpers/api_client.py` (modify — add artifact API methods)
  - `tests/backend-integration/conftest.py` (modify — add LocalStack setup for E2E if needed)
- **Change type:** new tests + helper modifications

## Dependencies

- **Must complete first:** All implementation tasks (1-7)
- **Provides output to:** None (final task)
- **Shared interfaces/contracts:** All Track 1 interfaces tested end-to-end

## Implementation Specification

### Step 1: Add artifact methods to API client helper

Add the following methods to `tests/backend-integration/helpers/api_client.py`:

```python
    def list_artifacts(self, task_id, direction=None, expected_status=200, raise_for_status=True):
        """List artifacts for a task."""
        params = {}
        if direction:
            params["direction"] = direction
        query = "&".join(f"{k}={v}" for k, v in params.items())
        path = f"/tasks/{task_id}/artifacts{'?' + query if query else ''}"
        return self._request("GET", path, expected_status=expected_status,
                           raise_for_status=raise_for_status)

    def download_artifact(self, task_id, filename, direction="output",
                         expected_status=200, raise_for_status=True):
        """Download an artifact file. Returns raw response body bytes."""
        params = {"direction": direction}
        query = "&".join(f"{k}={v}" for k, v in params.items())
        path = f"/tasks/{task_id}/artifacts/{filename}?{query}"

        url = f"{self.base}{path}"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                status_code = response.status
                content_type = response.headers.get("Content-Type", "")
                body = response.read()
                if raise_for_status and status_code != expected_status:
                    raise ApiError(status_code, {"error": body.decode("utf-8", errors="replace")})
                return {
                    "status_code": status_code,
                    "body": body,
                    "content_type": content_type,
                }
        except urllib.error.HTTPError as exc:
            if raise_for_status:
                raise ApiError(exc.code, {"error": exc.read().decode("utf-8", errors="replace")})
            return {
                "status_code": exc.code,
                "body": exc.read(),
                "content_type": "",
            }
```

### Step 2: Add upload_artifact mock LLM behavior

Add a helper to `tests/backend-integration/helpers/mock_llm.py` that produces `upload_artifact` tool calls:

```python
def upload_artifact_call(filename: str, content: str, content_type: str = "text/plain",
                         final_answer: str = "Artifact uploaded successfully."):
    """Create a mock LLM that calls upload_artifact, then responds with a final answer.

    Returns a callable that can be passed to e2e.use_llm().
    """
    def handler(messages, **kwargs):
        # Check if upload_artifact result is already in messages
        for msg in messages:
            if hasattr(msg, "type") and msg.type == "tool":
                if hasattr(msg, "name") and msg.name == "upload_artifact":
                    # Tool already called; return final answer
                    return AIMessage(content=final_answer)

        # First call — invoke upload_artifact
        return AIMessage(
            content="",
            tool_calls=[
                ToolCall(
                    id="upload_artifact_1",
                    name="upload_artifact",
                    args={
                        "filename": filename,
                        "content": content,
                        "content_type": content_type,
                    },
                )
            ],
        )

    return handler
```

Ensure the `AIMessage` and `ToolCall` imports are present at the top of `mock_llm.py`:

```python
from langchain_core.messages import AIMessage, ToolCall
```

### Step 3: Create artifact integration tests

Create `tests/backend-integration/test_artifacts.py`:

```python
"""Integration tests for Track 1: Output Artifact Storage.

Tests the end-to-end artifact flow:
1. Agent calls upload_artifact tool during task execution
2. Artifact metadata appears in list endpoint
3. Artifact file can be downloaded with correct content
"""

import pytest

from helpers.mock_llm import upload_artifact_call


@pytest.mark.asyncio
async def test_upload_and_list_artifact(e2e):
    """Agent uploads an artifact via upload_artifact tool.
    Verify it appears in the artifact list endpoint."""

    artifact_content = "# Analysis Report\n\nThis is a test report.\n"
    artifact_filename = "report.md"

    e2e.use_llm(upload_artifact_call(
        filename=artifact_filename,
        content=artifact_content,
        content_type="text/markdown",
        final_answer="Report has been saved as report.md.",
    ))
    await e2e.start_worker("e2e-artifact-worker")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant that produces reports.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["upload_artifact"],
    })

    task_id = e2e.submit_task(input="Write a short analysis report.")

    completed = await e2e.wait_for_status(task_id, "completed", timeout=30.0)
    assert "report" in str(completed["output"]["result"]).lower()

    # List artifacts
    artifacts_response = e2e.api.list_artifacts(task_id)
    artifacts = artifacts_response["body"]
    assert len(artifacts) == 1

    artifact = artifacts[0]
    assert artifact["filename"] == artifact_filename
    assert artifact["direction"] == "output"
    assert artifact["contentType"] == "text/markdown"
    assert artifact["sizeBytes"] == len(artifact_content.encode("utf-8"))


@pytest.mark.asyncio
async def test_download_artifact_content(e2e):
    """After an artifact is uploaded, download it and verify the content matches."""

    artifact_content = "col1,col2,col3\n1,2,3\n4,5,6\n"
    artifact_filename = "data.csv"

    e2e.use_llm(upload_artifact_call(
        filename=artifact_filename,
        content=artifact_content,
        content_type="text/csv",
    ))
    await e2e.start_worker("e2e-artifact-dl-worker")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["upload_artifact"],
    })

    task_id = e2e.submit_task(input="Generate a CSV file.")
    await e2e.wait_for_status(task_id, "completed", timeout=30.0)

    # Download artifact
    download = e2e.api.download_artifact(task_id, artifact_filename)
    assert download["status_code"] == 200
    assert download["body"].decode("utf-8") == artifact_content
    assert "text/csv" in download["content_type"]


@pytest.mark.asyncio
async def test_list_artifacts_empty(e2e):
    """List artifacts for a task with no artifacts returns empty list."""

    from helpers.mock_llm import simple_response

    e2e.use_llm(simple_response("Done, no artifacts."))
    await e2e.start_worker("e2e-artifact-empty-worker")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": [],
    })

    task_id = e2e.submit_task(input="Just respond.")
    await e2e.wait_for_status(task_id, "completed", timeout=20.0)

    # List artifacts — should be empty
    artifacts_response = e2e.api.list_artifacts(task_id)
    artifacts = artifacts_response["body"]
    assert artifacts == []


@pytest.mark.asyncio
async def test_download_nonexistent_artifact_returns_404(e2e):
    """Download a non-existent artifact returns 404."""

    from helpers.mock_llm import simple_response

    e2e.use_llm(simple_response("Done."))
    await e2e.start_worker("e2e-artifact-404-worker")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": [],
    })

    task_id = e2e.submit_task(input="Just respond.")
    await e2e.wait_for_status(task_id, "completed", timeout=20.0)

    # Download non-existent artifact — should be 404
    download = e2e.api.download_artifact(
        task_id, "nonexistent.pdf",
        expected_status=404, raise_for_status=False,
    )
    assert download["status_code"] == 404


@pytest.mark.asyncio
async def test_list_artifacts_with_direction_filter(e2e):
    """List artifacts with direction filter returns only matching artifacts."""

    artifact_content = "Test content"

    e2e.use_llm(upload_artifact_call(
        filename="filtered.txt",
        content=artifact_content,
        content_type="text/plain",
    ))
    await e2e.start_worker("e2e-artifact-filter-worker")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["upload_artifact"],
    })

    task_id = e2e.submit_task(input="Create a text file.")
    await e2e.wait_for_status(task_id, "completed", timeout=30.0)

    # Filter by output — should return the artifact
    output_artifacts = e2e.api.list_artifacts(task_id, direction="output")
    assert len(output_artifacts["body"]) == 1

    # Filter by input — should return empty (no input artifacts in Track 1)
    input_artifacts = e2e.api.list_artifacts(task_id, direction="input")
    assert len(input_artifacts["body"]) == 0
```

### Step 4: Add LocalStack configuration for E2E infrastructure

The E2E test infrastructure needs S3 (LocalStack) for artifact storage. Update the E2E setup to ensure LocalStack is available.

Add LocalStack environment variables to the E2E conftest (or worker launch config) in `tests/backend-integration/conftest.py`. The worker process needs these environment variables:

```python
# Add to the environment variables passed to the E2E worker:
"S3_ENDPOINT_URL": os.getenv("E2E_S3_ENDPOINT_URL", "http://localhost:4566"),
"S3_BUCKET_NAME": os.getenv("E2E_S3_BUCKET_NAME", "platform-artifacts"),
"AWS_ACCESS_KEY_ID": "test",
"AWS_SECRET_ACCESS_KEY": "test",
"AWS_REGION": "us-east-1",
```

Also add the S3 endpoint to the E2E API service's environment (in the API startup command in conftest.py or Makefile):

```
S3_ENDPOINT_URL=http://localhost:4566
S3_BUCKET_NAME=platform-artifacts
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
AWS_REGION=us-east-1
```

**Note:** The E2E infrastructure reuses the same LocalStack container started by `make db-up` (port 4566). If E2E tests need an isolated LocalStack, a separate container can be added to the E2E Makefile targets, but reusing the dev LocalStack is simpler and sufficient for Track 1.

### Step 5: Update Makefile E2E targets for LocalStack

Ensure that `make e2e-up` starts LocalStack alongside the E2E PostgreSQL container. If the E2E infrastructure doesn't already include LocalStack, add it. The simplest approach is to ensure `make db-up` has been run before `make e2e-test`, since LocalStack runs on a fixed port (4566).

Add a note or dependency to the E2E targets:

```makefile
# Ensure LocalStack is running before E2E tests
e2e-test: db-up
```

Or add an explicit LocalStack check at the start of the E2E test target.

## Acceptance Criteria

- [ ] `tests/backend-integration/test_artifacts.py` exists with integration tests
- [ ] Test 1: Agent creates artifact via `upload_artifact` tool → artifact appears in list endpoint with correct metadata (filename, direction, content_type, size_bytes)
- [ ] Test 2: Downloaded artifact content matches the original content, with correct Content-Type header
- [ ] Test 3: List artifacts for task with no artifacts returns empty list
- [ ] Test 4: Download non-existent artifact returns 404
- [ ] Test 5: List artifacts with direction filter returns only matching artifacts
- [ ] `api_client.py` has `list_artifacts()` and `download_artifact()` helper methods
- [ ] `mock_llm.py` has `upload_artifact_call()` helper for creating mock LLM behaviors
- [ ] E2E worker environment includes `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`, AWS credentials
- [ ] E2E API environment includes `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`, AWS credentials
- [ ] All integration tests pass with `make e2e-test`
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Integration tests:** All 5 test scenarios listed above must pass.
- **Infrastructure:** Tests run against isolated E2E PostgreSQL (port 55433) and LocalStack S3 (port 4566).
- **Isolation:** Each test uses its own agent and task IDs. Existing DB cleanup in conftest handles data isolation.
- **Regression:** All existing E2E tests must still pass — the new tests are additive.

## Constraints and Guardrails

- Do not mock S3 in integration tests — use real LocalStack.
- Do not mock the database — use real E2E PostgreSQL.
- Do not create a separate LocalStack container for E2E — reuse the one from `make db-up`.
- Follow existing E2E test patterns: `e2e` fixture, `e2e.use_llm()`, `e2e.start_worker()`, `e2e.ensure_agent()`, `e2e.submit_task()`, `e2e.wait_for_status()`.
- Use `e2e.api.list_artifacts()` and `e2e.api.download_artifact()` for API assertions.
- Test names should follow the existing naming convention: `test_<scenario_description>`.

## Assumptions

- All implementation tasks (1-7) have been completed and the full artifact flow is functional.
- The E2E infrastructure (`make e2e-up`) starts PostgreSQL on port 55433 and the API on port 8081.
- LocalStack is running on port 4566 (started by `make db-up` or a dedicated E2E target).
- The `platform-artifacts` S3 bucket exists in LocalStack (created by `init-localstack.sh`).
- The mock LLM infrastructure supports `upload_artifact` tool calls (LangGraph dispatches the tool call to the registered tool handler).
- The `e2e` fixture handles cleanup of test data between tests via the existing `cleanup_test_db()` function. Artifact rows in `task_artifacts` are cleaned by the `DELETE FROM tasks` cascade (FK on `task_id`) or need explicit cleanup.

<!-- AGENT_TASK_END: task-8-integration-tests.md -->
