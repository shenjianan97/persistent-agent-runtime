<!-- AGENT_TASK_START: task-6-upload-artifact-tool.md -->

# Task 6 — `upload_artifact` Built-In Tool

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 1: Built-in sandbox tools — `upload_artifact` description, Section 2: Artifact Storage — Output artifact upload)
2. `docs/exec-plans/active/agent-capabilities/track-1/plan.md` — Track 1 execution plan
3. `services/worker-service/tools/definitions.py` — existing tool definitions, `ToolDefinition` dataclass, `ToolDependencies`, argument/result models
4. `services/worker-service/executor/graph.py` lines 138-212 — `_get_tools()` method showing how built-in tools are registered
5. `services/worker-service/storage/s3_client.py` — Task 3 output: `S3Client` class with `upload()`, `build_key()`

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-1/progress.md` to "Done".

## Context

The `upload_artifact` tool enables agents to produce output files (reports, data files, code archives) without requiring a sandbox. When an agent calls this tool, the worker:

1. Validates the artifact content size
2. Encodes the content string to bytes
3. Uploads the bytes to S3 via `S3Client`
4. Inserts a `task_artifacts` row via asyncpg
5. Returns confirmation to the agent

This tool works with or without a sandbox — a research agent can produce a markdown report, a data analysis agent can output a CSV, etc.

## Task-Specific Shared Contract

- Tool name: `upload_artifact`
- Arguments: `filename` (str, max 255 chars), `content` (str, max 50 MB / 52428800 bytes after encoding), `content_type` (str, max 100 chars, default `text/plain`)
- Result: `filename` (str), `size_bytes` (int), `content_type` (str)
- S3 key: `{tenant_id}/{task_id}/output/{filename}` (always `output` direction)
- DB insert: into `task_artifacts` table with all required columns
- The tool needs access to: `S3Client` instance, asyncpg connection pool, `task_id`, `tenant_id` — all passed via closure in `_get_tools()`
- Registered conditionally when `"upload_artifact"` is in `allowed_tools`

## Affected Component

- **Service/Module:** Worker Service — Tools and Executor
- **File paths:**
  - `services/worker-service/tools/upload_artifact.py` (new — tool argument/result models and implementation)
  - `services/worker-service/tools/definitions.py` (modify — add `UPLOAD_ARTIFACT_TOOL` constant)
  - `services/worker-service/executor/graph.py` (modify — register `upload_artifact` in `_get_tools()`)
  - `services/worker-service/tests/tools/__init__.py` (new — empty test package init)
  - `services/worker-service/tests/tools/test_upload_artifact.py` (new — unit tests)
  - `services/api-service/src/main/java/com/persistentagent/api/config/ValidationConstants.java` (modify — add `upload_artifact` to `ALLOWED_TOOLS`)
- **Change type:** new code + modification

## Dependencies

- **Must complete first:** Task 1 (DB Migration — `task_artifacts` table), Task 3 (Worker S3 Client — `S3Client` class)
- **Provides output to:** Task 7 (Console — artifacts are visible after upload), Task 8 (Integration Tests)
- **Shared interfaces/contracts:** `S3Client.upload()`, `S3Client.build_key()`, asyncpg pool for DB operations

## Implementation Specification

### Step 1: Create upload_artifact module

Create `services/worker-service/tools/upload_artifact.py`:

```python
"""upload_artifact built-in tool — allows agents to produce output files."""

from __future__ import annotations

from typing import Annotated

import asyncpg
import structlog
from pydantic import BaseModel, Field

from storage.s3_client import S3Client

logger = structlog.get_logger(__name__)

MAX_CONTENT_BYTES = 52_428_800  # 50 MB
MAX_FILENAME_LENGTH = 255
MAX_CONTENT_TYPE_LENGTH = 100


class UploadArtifactArguments(BaseModel):
    """Arguments for the upload_artifact tool."""

    filename: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_FILENAME_LENGTH,
            description="Name for the output file (e.g., 'report.pdf', 'data.csv').",
        ),
    ]
    content: Annotated[
        str,
        Field(
            min_length=1,
            description="The file content as a string. For binary files, use base64 encoding.",
        ),
    ]
    content_type: Annotated[
        str,
        Field(
            max_length=MAX_CONTENT_TYPE_LENGTH,
            default="text/plain",
            description="MIME type of the content (e.g., 'text/plain', 'application/json', 'text/csv').",
        ),
    ]


class UploadArtifactResult(BaseModel):
    """Result returned after a successful artifact upload."""

    filename: str
    size_bytes: int
    content_type: str


async def execute_upload_artifact(
    *,
    filename: str,
    content: str,
    content_type: str = "text/plain",
    s3_client: S3Client,
    pool: asyncpg.Pool,
    task_id: str,
    tenant_id: str,
) -> dict:
    """Execute the upload_artifact tool.

    Encodes content to bytes, uploads to S3, and inserts a task_artifacts row.

    Args:
        filename: Name for the output file
        content: File content as a string
        content_type: MIME type of the content
        s3_client: S3 client for file upload
        pool: asyncpg pool for DB operations
        task_id: ID of the current task
        tenant_id: ID of the current tenant

    Returns:
        Dict with filename, size_bytes, and content_type

    Raises:
        ValueError: If content exceeds the 50 MB size limit
    """
    # Encode content to bytes
    data = content.encode("utf-8")
    size_bytes = len(data)

    # Validate size
    if size_bytes > MAX_CONTENT_BYTES:
        raise ValueError(
            f"Artifact content too large: {size_bytes} bytes "
            f"(maximum {MAX_CONTENT_BYTES} bytes / 50 MB)"
        )

    # Build S3 key
    s3_key = s3_client.build_key(
        tenant_id=tenant_id,
        task_id=task_id,
        direction="output",
        filename=filename,
    )

    logger.info(
        "upload_artifact_started",
        task_id=task_id,
        tenant_id=tenant_id,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        s3_key=s3_key,
    )

    # Upload to S3
    await s3_client.upload(key=s3_key, data=data, content_type=content_type)

    # Insert artifact metadata into database
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO task_artifacts (task_id, tenant_id, filename, direction,
                                        content_type, size_bytes, s3_key)
            VALUES ($1, $2, $3, 'output', $4, $5, $6)
            ON CONFLICT (task_id, direction, filename)
            DO UPDATE SET content_type = EXCLUDED.content_type,
                          size_bytes = EXCLUDED.size_bytes,
                          s3_key = EXCLUDED.s3_key,
                          created_at = NOW()
            """,
            task_id,
            tenant_id,
            filename,
            content_type,
            size_bytes,
            s3_key,
        )

    logger.info(
        "upload_artifact_completed",
        task_id=task_id,
        tenant_id=tenant_id,
        filename=filename,
        size_bytes=size_bytes,
    )

    return {
        "filename": filename,
        "size_bytes": size_bytes,
        "content_type": content_type,
    }
```

### Step 2: Add UPLOAD_ARTIFACT_TOOL to definitions.py

Add the following to `services/worker-service/tools/definitions.py`, after the existing `DEV_SLEEP_TOOL` definition:

```python
# Import at the top of the file (add to existing imports)
from tools.upload_artifact import UploadArtifactArguments, UploadArtifactResult

# Add after DEV_SLEEP_TOOL definition
UPLOAD_ARTIFACT_TOOL = ToolDefinition(
    name="upload_artifact",
    description="Save content as an output artifact file. The file will be available for download via the API after task completion.",
    input_model=UploadArtifactArguments,
    output_model=UploadArtifactResult,
)
```

### Step 3: Register upload_artifact in _get_tools()

Add the `upload_artifact` tool registration to the `_get_tools()` method in `services/worker-service/executor/graph.py`. Add it after the existing `dev_sleep` registration block and before `return tools`:

```python
        if "upload_artifact" in allowed_tools:
            from tools.upload_artifact import (
                UploadArtifactArguments,
                execute_upload_artifact,
            )
            from tools.definitions import UPLOAD_ARTIFACT_TOOL

            async def upload_artifact(
                filename: str,
                content: str,
                content_type: str = "text/plain",
            ):
                return await execute_upload_artifact(
                    filename=filename,
                    content=content,
                    content_type=content_type,
                    s3_client=self.s3_client,
                    pool=self.pool,
                    task_id=task_id,
                    tenant_id=tenant_id,
                )

            tools.append(
                StructuredTool.from_function(
                    coroutine=upload_artifact,
                    name="upload_artifact",
                    description=UPLOAD_ARTIFACT_TOOL.description,
                    args_schema=UploadArtifactArguments,
                )
            )
```

**Important:** The `_get_tools()` method signature must be updated to accept `tenant_id` and `task_id` parameters. Update the method signature:

```python
    def _get_tools(
        self,
        allowed_tools: list[str],
        *,
        cancel_event: asyncio.Event,
        task_id: str,
        tenant_id: str = "default",
    ) -> list[StructuredTool]:
```

Also, update all call sites of `_get_tools()` to pass `tenant_id`. The primary call site is in `_build_graph()`:

```python
        tools = self._get_tools(
            allowed_tools,
            cancel_event=cancel_event,
            task_id=task_id,
            tenant_id=tenant_id,
        )
```

The `_build_graph()` method signature must also accept `tenant_id`:

```python
    async def _build_graph(
        self,
        agent_config: dict[str, Any],
        *,
        cancel_event: asyncio.Event,
        task_id: str,
        tenant_id: str = "default",
        custom_tools: list[StructuredTool] | None = None,
    ) -> StateGraph:
```

And the `execute_task()` call to `_build_graph()` must pass `tenant_id`:

```python
        graph = await self._build_graph(
            agent_config,
            cancel_event=cancel_event,
            task_id=task_id,
            tenant_id=tenant_id,
            custom_tools=custom_tools if custom_tools else None,
        )
```

**S3Client initialization:** The `GraphExecutor.__init__()` method (or a setup method) must create an `S3Client` instance and store it as `self.s3_client`. Add this to the executor initialization:

```python
from storage.s3_client import S3Client
import os

# In GraphExecutor.__init__() or a setup method:
s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL")
s3_bucket_name = os.environ.get("S3_BUCKET_NAME", "platform-artifacts")
self.s3_client = S3Client(
    endpoint_url=s3_endpoint_url,
    bucket_name=s3_bucket_name,
)
```

### Step 4: Create test package init

Create `services/worker-service/tests/tools/__init__.py`:

```python
```

(Empty file — test package init only.)

### Step 5: Create unit tests

Create `services/worker-service/tests/tools/test_upload_artifact.py`:

```python
"""Unit tests for the upload_artifact tool."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.upload_artifact import (
    MAX_CONTENT_BYTES,
    MAX_FILENAME_LENGTH,
    UploadArtifactArguments,
    UploadArtifactResult,
    execute_upload_artifact,
)


class TestUploadArtifactArguments:
    def test_valid_arguments(self):
        args = UploadArtifactArguments(
            filename="report.pdf",
            content="file content here",
            content_type="application/pdf",
        )
        assert args.filename == "report.pdf"
        assert args.content == "file content here"
        assert args.content_type == "application/pdf"

    def test_default_content_type(self):
        args = UploadArtifactArguments(
            filename="output.txt",
            content="some text",
        )
        assert args.content_type == "text/plain"

    def test_filename_too_long_rejected(self):
        with pytest.raises(Exception):
            UploadArtifactArguments(
                filename="x" * (MAX_FILENAME_LENGTH + 1),
                content="content",
            )

    def test_empty_filename_rejected(self):
        with pytest.raises(Exception):
            UploadArtifactArguments(
                filename="",
                content="content",
            )

    def test_empty_content_rejected(self):
        with pytest.raises(Exception):
            UploadArtifactArguments(
                filename="file.txt",
                content="",
            )


class TestUploadArtifactResult:
    def test_result_fields(self):
        result = UploadArtifactResult(
            filename="report.pdf",
            size_bytes=1024,
            content_type="application/pdf",
        )
        assert result.filename == "report.pdf"
        assert result.size_bytes == 1024
        assert result.content_type == "application/pdf"


class TestExecuteUploadArtifact:
    @pytest.mark.asyncio
    async def test_successful_upload(self):
        """upload_artifact should upload to S3 and insert DB row."""
        mock_s3 = MagicMock()
        mock_s3.upload = AsyncMock()
        mock_s3.build_key.return_value = "tenant-1/task-abc/output/report.txt"

        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await execute_upload_artifact(
            filename="report.txt",
            content="Hello, world!",
            content_type="text/plain",
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-abc",
            tenant_id="tenant-1",
        )

        assert result["filename"] == "report.txt"
        assert result["size_bytes"] == len("Hello, world!".encode("utf-8"))
        assert result["content_type"] == "text/plain"

        # Verify S3 upload was called
        mock_s3.build_key.assert_called_once_with(
            tenant_id="tenant-1",
            task_id="task-abc",
            direction="output",
            filename="report.txt",
        )
        mock_s3.upload.assert_called_once_with(
            key="tenant-1/task-abc/output/report.txt",
            data=b"Hello, world!",
            content_type="text/plain",
        )

        # Verify DB insert was called
        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args
        assert "INSERT INTO task_artifacts" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_content_too_large_raises_error(self):
        """upload_artifact should reject content exceeding 50 MB."""
        mock_s3 = MagicMock()
        mock_s3.build_key.return_value = "key"
        mock_pool = MagicMock()

        # Create content that exceeds MAX_CONTENT_BYTES
        large_content = "x" * (MAX_CONTENT_BYTES + 1)

        with pytest.raises(ValueError, match="Artifact content too large"):
            await execute_upload_artifact(
                filename="huge.txt",
                content=large_content,
                content_type="text/plain",
                s3_client=mock_s3,
                pool=mock_pool,
                task_id="task-abc",
                tenant_id="tenant-1",
            )

        # Verify S3 upload was NOT called
        mock_s3.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_upsert_on_duplicate_filename(self):
        """upload_artifact should use ON CONFLICT to handle duplicate filenames."""
        mock_s3 = MagicMock()
        mock_s3.upload = AsyncMock()
        mock_s3.build_key.return_value = "tenant-1/task-abc/output/data.csv"

        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        await execute_upload_artifact(
            filename="data.csv",
            content="col1,col2\n1,2",
            content_type="text/csv",
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-abc",
            tenant_id="tenant-1",
        )

        # Verify the SQL uses ON CONFLICT
        call_args = mock_conn.execute.call_args
        assert "ON CONFLICT" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_default_content_type(self):
        """upload_artifact should default to text/plain when content_type not specified."""
        mock_s3 = MagicMock()
        mock_s3.upload = AsyncMock()
        mock_s3.build_key.return_value = "t/task/output/file.txt"

        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await execute_upload_artifact(
            filename="file.txt",
            content="hello",
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task",
            tenant_id="t",
        )

        assert result["content_type"] == "text/plain"
        mock_s3.upload.assert_called_once_with(
            key="t/task/output/file.txt",
            data=b"hello",
            content_type="text/plain",
        )
```

### Step 6: Add upload_artifact to ValidationConstants ALLOWED_TOOLS

Track 1 must be independently deployable, so `upload_artifact` must be accepted by the API validation layer. Add `"upload_artifact"` to the `ALLOWED_TOOLS` set in `services/api-service/src/main/java/com/persistentagent/api/config/ValidationConstants.java`:

```java
/** Stable public tools available in all environments. */
public static final Set<String> ALLOWED_TOOLS = Set.of("web_search", "read_url", "calculator", "request_human_input", "upload_artifact");
```

This ensures agents can be configured with `upload_artifact` in their tool list without failing validation.

## Acceptance Criteria

- [ ] `services/worker-service/tools/upload_artifact.py` exists with `UploadArtifactArguments`, `UploadArtifactResult`, and `execute_upload_artifact()`
- [ ] `UploadArtifactArguments` validates: `filename` (1-255 chars), `content` (min 1 char), `content_type` (max 100 chars, default `text/plain`)
- [ ] `execute_upload_artifact()` encodes content to UTF-8 bytes
- [ ] Content size validated against 50 MB limit (52,428,800 bytes) — raises `ValueError` if exceeded
- [ ] S3 upload uses `S3Client.upload()` with correct key from `build_key()`
- [ ] DB insert uses `INSERT INTO task_artifacts` with `ON CONFLICT` upsert for duplicate filenames
- [ ] Direction is always `'output'` (hardcoded)
- [ ] `UPLOAD_ARTIFACT_TOOL` constant added to `definitions.py`
- [ ] Tool registered in `_get_tools()` when `"upload_artifact"` in `allowed_tools`
- [ ] `_get_tools()` updated to accept `tenant_id` parameter
- [ ] `_build_graph()` passes `tenant_id` to `_get_tools()`
- [ ] `GraphExecutor` creates `S3Client` instance as `self.s3_client`
- [ ] `"upload_artifact"` added to `ALLOWED_TOOLS` in `ValidationConstants.java`
- [ ] All unit tests pass
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests (Arguments):** Valid arguments accepted. Default `content_type` is `text/plain`. Filename too long rejected. Empty filename rejected. Empty content rejected.
- **Unit tests (Execute):** Successful upload calls S3 and DB. Content exceeding 50 MB raises `ValueError`. Duplicate filename uses upsert. Default content_type applied.
- **Regression tests:** Run `make test` — all existing executor and tool tests must still pass, particularly the existing `_get_tools()` tests.

## Constraints and Guardrails

- Do not add binary file support (base64 decoding) — content is always UTF-8 encoded in Track 1. Binary support can be added later.
- Do not add file size limits per task (200 MB total) — that is enforced at a higher level later.
- Do not modify existing tool registrations (web_search, read_url, etc.) — only add the new `upload_artifact` block.
- The tool implementation must be async-compatible — all I/O through async calls.
- Use `structlog` for logging, not the standard `logging` module.
- Use `ON CONFLICT` upsert in the DB insert to handle duplicate filenames gracefully (overwrite rather than error).

## Assumptions

- Task 1 has been completed and `task_artifacts` table exists with the expected schema.
- Task 3 has been completed and `S3Client` class is available at `storage.s3_client`.
- `GraphExecutor` has access to `self.pool` (asyncpg connection pool) from existing initialization.
- `self.s3_client` can be created during executor initialization using `S3_ENDPOINT_URL` and `S3_BUCKET_NAME` environment variables.
- The `task_id` and `tenant_id` are available in `execute_task()` and can be threaded through to `_get_tools()` via `_build_graph()`.
- Existing tests that call `_get_tools()` will need to pass the new `tenant_id` parameter (or accept its default value of `"default"`).

<!-- AGENT_TASK_END: task-6-upload-artifact-tool.md -->
