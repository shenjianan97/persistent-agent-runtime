<!-- AGENT_TASK_START: task-5-sandbox-download-tool.md -->

# Task 5 — sandbox_download Built-in Tool

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 2: output artifact upload from sandbox)
2. `docs/exec-plans/active/agent-capabilities/track-2/plan.md` — Track 2 execution plan
3. `docs/exec-plans/active/agent-capabilities/track-1/plan.md` — Track 1 plan (artifact service and S3 client)
4. `services/worker-service/tools/sandbox_tools.py` — Tasks 3-4 output: existing sandbox tools
5. `services/worker-service/storage/s3_client.py` — Track 1 output: S3Client class (async methods using asyncio.to_thread internally)
6. `services/worker-service/executor/graph.py` — `_get_tools()` for registration pattern

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-2/progress.md` to "Done".

## Context

The `sandbox_download` tool allows the LLM agent to export a file from the E2B sandbox as an output artifact. When called, the tool:
1. Reads the file from the sandbox filesystem
2. Determines the filename and content type
3. Uploads to S3 via Track 1's S3Client
4. Inserts a `task_artifacts` row via asyncpg

This bridges the sandbox world with the artifact storage system. The agent decides which files become output artifacts by explicitly calling this tool.

## Task-Specific Shared Contract

- Tool name: `sandbox_download`
- Arguments: `path` (str, max 1000), `filename` (str|None, max 255, defaults to basename of path)
- Result: `filename` (str), `size_bytes` (int), `content_type` (str)
- Uses Track 1's `S3Client.upload()` for S3 upload — do NOT create separate S3 code
- S3 key format: `{tenant_id}/{task_id}/output/{filename}` — matches Track 1 convention
- Inserts `task_artifacts` row with direction='output'
- The tool needs: sandbox instance, S3Client, asyncpg pool, task_id, tenant_id — all via closure
- Content type inferred from file extension via `mimetypes.guess_type()`
- Conditional on `sandbox.enabled: true` AND `"sandbox_download"` in `allowed_tools`

## Affected Component

- **Service/Module:** Worker Service — Sandbox Tools
- **File paths:**
  - `services/worker-service/tools/sandbox_tools.py` (modify — add sandbox_download tool)
  - `services/worker-service/tools/definitions.py` (modify — add SANDBOX_DOWNLOAD_TOOL definition)
  - `services/worker-service/executor/graph.py` (modify — register sandbox_download in `_get_tools()`)
  - `services/worker-service/tests/test_sandbox_tools.py` (modify — add tests)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 3 (sandbox_exec — establishes sandbox_tools.py), Track 1 Task 3 (S3Client), Track 1 Task 1 (task_artifacts table)
- **Provides output to:** Task 9 (Integration Tests)
- **Shared interfaces/contracts:** Track 1's S3Client; asyncpg pool for task_artifacts inserts; sandbox_tools.py module

## Implementation Specification

### Step 1: Add sandbox_download to sandbox_tools.py

Add to `services/worker-service/tools/sandbox_tools.py` after the `sandbox_write_file` section:

```python
# --- sandbox_download ---

class SandboxDownloadArguments(BaseModel):
    path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1000,
            description="Path in the sandbox filesystem to download as an output artifact.",
        ),
    ]
    filename: Annotated[
        str | None,
        Field(
            default=None,
            max_length=255,
            description="Output artifact filename. Defaults to the basename of the path.",
        ),
    ] = None


class SandboxDownloadResult(BaseModel):
    filename: str
    size_bytes: int
    content_type: str


def create_sandbox_download_fn(sandbox, *, s3_client, pool, task_id: str, tenant_id: str):
    """Create the sandbox_download async function with dependencies bound via closure.

    Args:
        sandbox: E2B Sandbox instance
        s3_client: Track 1 S3Client instance for uploading to artifact storage
        pool: asyncpg connection pool for inserting task_artifacts rows
        task_id: UUID string of the current task
        tenant_id: tenant ID for S3 key construction

    Returns:
        Async function suitable for StructuredTool.from_function(coroutine=...)
    """
    import os

    async def sandbox_download(path: str, filename: str | None = None) -> dict:
        start_time = time.monotonic()
        try:
            # 1. Read file from sandbox
            data = await asyncio.to_thread(sandbox.files.read, path)

            # Ensure we have bytes
            if isinstance(data, str):
                data = data.encode("utf-8")

            size_bytes = len(data)

            # 2. Determine filename
            effective_filename = filename or os.path.basename(path)
            if not effective_filename:
                effective_filename = "download"

            # 3. Infer content type from file extension
            content_type, _ = mimetypes.guess_type(effective_filename)
            if content_type is None:
                content_type = "application/octet-stream"

            # 4. Upload to S3 via Track 1's S3Client
            s3_key = s3_client.build_key(tenant_id, task_id, "output", effective_filename)
            await s3_client.upload(s3_key, data, content_type)

            # 5. Insert task_artifacts row
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO task_artifacts (task_id, tenant_id, filename, direction, content_type, size_bytes, s3_key)
                       VALUES ($1::uuid, $2, $3, 'output', $4, $5, $6)
                       ON CONFLICT (task_id, direction, filename) DO UPDATE
                       SET content_type = EXCLUDED.content_type,
                           size_bytes = EXCLUDED.size_bytes,
                           s3_key = EXCLUDED.s3_key""",
                    task_id,
                    tenant_id,
                    effective_filename,
                    content_type,
                    size_bytes,
                    s3_key,
                )

            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_download_completed",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "task_id": task_id,
                    "path": path,
                    "filename": effective_filename,
                    "size_bytes": size_bytes,
                    "content_type": content_type,
                    "duration_ms": duration_ms,
                },
            )

            return {
                "filename": effective_filename,
                "size_bytes": size_bytes,
                "content_type": content_type,
            }

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "sandbox_download_error",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "task_id": task_id,
                    "path": path,
                    "error": str(e),
                    "duration_ms": duration_ms,
                },
            )
            return {
                "filename": filename or os.path.basename(path) or "download",
                "size_bytes": 0,
                "content_type": "application/octet-stream",
            }

    return sandbox_download
```

### Step 2: Add SANDBOX_DOWNLOAD_TOOL to definitions.py

Modify `services/worker-service/tools/definitions.py`:

```python
from tools.sandbox_tools import (
    SandboxExecArguments, SandboxExecResult,
    SandboxReadFileArguments, SandboxReadFileResult,
    SandboxWriteFileArguments, SandboxWriteFileResult,
    SandboxDownloadArguments, SandboxDownloadResult,
)

SANDBOX_DOWNLOAD_TOOL = ToolDefinition(
    name="sandbox_download",
    description="Download a file from the sandbox and save it as an output artifact. The file will be available via the task artifacts API.",
    input_model=SandboxDownloadArguments,
    output_model=SandboxDownloadResult,
)
```

### Step 3: Register sandbox_download in graph.py _get_tools()

Modify `services/worker-service/executor/graph.py`. Update the imports:

```python
from tools.sandbox_tools import (
    SandboxExecArguments,
    SandboxReadFileArguments,
    SandboxWriteFileArguments,
    SandboxDownloadArguments,
    create_sandbox_exec_fn,
    create_sandbox_read_file_fn,
    create_sandbox_write_file_fn,
    create_sandbox_download_fn,
)
from tools.definitions import (
    SANDBOX_EXEC_TOOL, SANDBOX_READ_FILE_TOOL,
    SANDBOX_WRITE_FILE_TOOL, SANDBOX_DOWNLOAD_TOOL,
)
```

Update `_get_tools()` signature to accept additional dependencies for sandbox_download:

```python
    def _get_tools(
        self,
        allowed_tools: list[str],
        *,
        cancel_event: asyncio.Event,
        task_id: str,
        sandbox=None,
        s3_client=None,
        tenant_id: str = "",
    ) -> list[StructuredTool]:
```

Add after the `sandbox_write_file` registration block:

```python
        if sandbox is not None and "sandbox_download" in allowed_tools and s3_client is not None:
            download_fn = create_sandbox_download_fn(
                sandbox,
                s3_client=s3_client,
                pool=self.pool,
                task_id=task_id,
                tenant_id=tenant_id,
            )

            async def sandbox_download_wrapper(path: str, filename: str | None = None):
                return await self._await_or_cancel(
                    download_fn(path, filename),
                    cancel_event,
                    task_id=task_id,
                    operation="sandbox_download",
                )

            tools.append(StructuredTool.from_function(
                coroutine=sandbox_download_wrapper,
                name="sandbox_download",
                description=SANDBOX_DOWNLOAD_TOOL.description,
                args_schema=SandboxDownloadArguments,
            ))
```

### Step 4: Write unit tests

Add to `services/worker-service/tests/test_sandbox_tools.py`:

```python
from tools.sandbox_tools import (
    SandboxDownloadArguments,
    SandboxDownloadResult,
    create_sandbox_download_fn,
)


class TestSandboxDownloadArguments:
    def test_valid_args(self):
        args = SandboxDownloadArguments(path="/home/user/report.pdf")
        assert args.path == "/home/user/report.pdf"
        assert args.filename is None

    def test_with_custom_filename(self):
        args = SandboxDownloadArguments(path="/home/user/output.txt", filename="report.txt")
        assert args.filename == "report.txt"

    def test_empty_path_rejected(self):
        with pytest.raises(Exception):
            SandboxDownloadArguments(path="")

    def test_long_filename_rejected(self):
        with pytest.raises(Exception):
            SandboxDownloadArguments(path="/file.txt", filename="x" * 256)


class TestSandboxDownloadResult:
    def test_result_fields(self):
        result = SandboxDownloadResult(filename="report.pdf", size_bytes=1024, content_type="application/pdf")
        assert result.filename == "report.pdf"
        assert result.size_bytes == 1024
        assert result.content_type == "application/pdf"


class TestCreateSandboxDownloadFn:
    @pytest.mark.asyncio
    async def test_download_success(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_s3 = MagicMock()
        mock_s3.build_key = MagicMock(return_value="default/task-123/output/report.pdf")
        mock_s3.upload = MagicMock()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        download_fn = create_sandbox_download_fn(
            mock_sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        mock_s3.upload = AsyncMock()

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = b"PDF content here"  # sandbox.files.read
            result = await download_fn("/home/user/report.pdf")

        assert result["filename"] == "report.pdf"
        assert result["size_bytes"] == len(b"PDF content here")
        assert result["content_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_download_custom_filename(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_s3 = MagicMock()
        mock_s3.build_key = MagicMock(return_value="default/task-123/output/custom.csv")
        mock_s3.upload = MagicMock()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        mock_s3.upload = AsyncMock()

        download_fn = create_sandbox_download_fn(
            mock_sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = b"csv data"  # sandbox.files.read
            result = await download_fn("/home/user/data.txt", "custom.csv")

        assert result["filename"] == "custom.csv"
        assert result["content_type"] == "text/csv"

    @pytest.mark.asyncio
    async def test_download_text_content_encoded(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_s3 = MagicMock()
        mock_s3.build_key = MagicMock(return_value="key")
        mock_s3.upload = MagicMock()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        mock_s3.upload = AsyncMock()

        download_fn = create_sandbox_download_fn(
            mock_sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = "text content"  # str returned from sandbox.files.read
            result = await download_fn("/home/user/file.txt")

        assert result["size_bytes"] == len("text content".encode("utf-8"))

    @pytest.mark.asyncio
    async def test_download_error_returns_result(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_s3 = MagicMock()
        mock_pool = AsyncMock()

        download_fn = create_sandbox_download_fn(
            mock_sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = FileNotFoundError("No such file")
            result = await download_fn("/home/user/missing.txt")

        assert result["size_bytes"] == 0
        assert result["filename"] == "missing.txt"

    @pytest.mark.asyncio
    async def test_download_unknown_extension(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_s3 = MagicMock()
        mock_s3.build_key = MagicMock(return_value="key")
        mock_s3.upload = MagicMock()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        mock_s3.upload = AsyncMock()

        download_fn = create_sandbox_download_fn(
            mock_sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = b"data"  # sandbox.files.read
            result = await download_fn("/home/user/file.xyz123")

        assert result["content_type"] == "application/octet-stream"


# Helper for async context manager mocking
class AsyncContextManager:
    def __init__(self, mock_obj):
        self._mock = mock_obj

    async def __aenter__(self):
        return self._mock

    async def __aexit__(self, *args):
        pass
```

## Acceptance Criteria

- [ ] `SandboxDownloadArguments` has `path` (str, max 1000) and `filename` (str|None, max 255, default None)
- [ ] `SandboxDownloadResult` has `filename` (str), `size_bytes` (int), `content_type` (str)
- [ ] `create_sandbox_download_fn()` reads file from sandbox, uploads to S3, inserts DB row
- [ ] File read from sandbox via `sandbox.files.read(path)` through `asyncio.to_thread()`
- [ ] S3 upload via Track 1's `S3Client.upload(key, data, content_type)` — called directly with `await` (S3Client methods are already async)
- [ ] S3 key built via `s3_client.build_key(tenant_id, task_id, "output", filename)`
- [ ] `task_artifacts` row inserted with direction='output', using ON CONFLICT to handle re-downloads
- [ ] Content type inferred from file extension via `mimetypes.guess_type()`, defaults to `application/octet-stream`
- [ ] Filename defaults to `os.path.basename(path)` when not provided
- [ ] Text content from sandbox converted to bytes before upload
- [ ] `SANDBOX_DOWNLOAD_TOOL` added to `definitions.py`
- [ ] Tool registered in `_get_tools()` conditional on `sandbox is not None` AND `"sandbox_download"` in `allowed_tools` AND `s3_client is not None`
- [ ] Tool invocations go through `_await_or_cancel()` for cancellation support
- [ ] Errors return result dict with size_bytes=0 — never raise to the LLM
- [ ] All unit tests pass
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests:** Argument validation. Download success with correct filename/size/content_type. Custom filename override. Text content encoded to bytes. Error returns zero-size result. Unknown extension defaults to octet-stream. DB insert called with correct parameters.
- **Regression:** `make test` — all existing tests pass.

## Constraints and Guardrails

- Do not create the S3 client — use Track 1's existing `S3Client` implementation.
- Do not modify Track 1 code (s3_client.py).
- Do not create the sandbox — the sandbox instance is provided via closure.
- Tool must never raise exceptions to the LLM — always return a result dict.
- `_get_tools()` must remain backward compatible — new parameters (`s3_client`, `tenant_id`) have defaults.
- The `ON CONFLICT` clause handles the case where the agent downloads the same file twice.

## Assumptions

- Track 1 Task 3 has been completed (`storage/s3_client.py` exists with `S3Client` class).
- `S3Client` has async methods: `async upload(key: str, data: bytes, content_type: str)` and sync `build_key(tenant_id, task_id, direction, filename)`. The async methods internally use `asyncio.to_thread()` to wrap sync boto3 calls — do NOT wrap them with `asyncio.to_thread()` again.
- Track 1 Task 1 has been completed (`task_artifacts` table exists with the UNIQUE constraint on `(task_id, direction, filename)`).
- The E2B `Sandbox.files.read(path)` returns str or bytes.
- `self.pool` on `GraphExecutor` provides an asyncpg connection pool.
- The `mimetypes` module is available in the standard library.

<!-- AGENT_TASK_END: task-5-sandbox-download-tool.md -->
