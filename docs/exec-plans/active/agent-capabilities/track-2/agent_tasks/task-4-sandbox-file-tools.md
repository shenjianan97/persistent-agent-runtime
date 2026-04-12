<!-- AGENT_TASK_START: task-4-sandbox-file-tools.md -->

# Task 4 — sandbox_read_file + sandbox_write_file Built-in Tools

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 1: built-in sandbox tools)
2. `docs/exec-plans/active/agent-capabilities/track-2/plan.md` — Track 2 execution plan
3. `services/worker-service/tools/sandbox_tools.py` — Task 3 output: existing sandbox_exec tool implementation (same file you will extend)
4. `services/worker-service/tools/definitions.py` — existing tool definitions
5. `services/worker-service/executor/graph.py` — `_get_tools()` for registration pattern

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-2/progress.md` to "Done".

## Context

The `sandbox_read_file` and `sandbox_write_file` tools allow the LLM agent to read and write files in the E2B sandbox filesystem. They are companion tools to `sandbox_exec` (Task 3) — together they give the agent full code execution and file manipulation capability.

Both tools follow the same pattern as `sandbox_exec`: they receive the sandbox instance via closure, are conditionally registered in `_get_tools()`, and never raise exceptions to the LLM.

## Task-Specific Shared Contract

- Tools added to the same file as `sandbox_exec`: `services/worker-service/tools/sandbox_tools.py`
- `sandbox_read_file`: reads a file from the sandbox, returns its content as text
- `sandbox_write_file`: writes content to a file path in the sandbox, returns the path and size
- Both tools use `asyncio.to_thread()` to wrap synchronous E2B SDK calls
- Both tools are conditional on `sandbox.enabled: true` AND tool name in `allowed_tools`
- Errors return descriptive messages — never raise to the LLM

## Affected Component

- **Service/Module:** Worker Service — Sandbox Tools
- **File paths:**
  - `services/worker-service/tools/sandbox_tools.py` (modify — add read/write tools)
  - `services/worker-service/tools/definitions.py` (modify — add tool definitions)
  - `services/worker-service/executor/graph.py` (modify — register tools in `_get_tools()`)
  - `services/worker-service/tests/test_sandbox_tools.py` (modify — add tests)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 3 (sandbox_exec — establishes `sandbox_tools.py` and `_get_tools()` sandbox pattern)
- **Provides output to:** Task 9 (Integration Tests)
- **Shared interfaces/contracts:** `sandbox_tools.py` module shared with Task 3 (sandbox_exec) and Task 5 (sandbox_download)

## Implementation Specification

### Step 1: Add sandbox_read_file to sandbox_tools.py

Add to `services/worker-service/tools/sandbox_tools.py` after the `sandbox_exec` section:

```python
# --- sandbox_read_file ---

class SandboxReadFileArguments(BaseModel):
    path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1000,
            description="Absolute or relative path of the file to read in the sandbox.",
        ),
    ]


class SandboxReadFileResult(BaseModel):
    path: str
    content: str


def create_sandbox_read_file_fn(sandbox):
    """Create the sandbox_read_file async function with the sandbox bound via closure.

    Args:
        sandbox: E2B Sandbox instance

    Returns:
        Async function suitable for StructuredTool.from_function(coroutine=...)
    """

    async def sandbox_read_file(path: str) -> dict:
        start_time = time.monotonic()
        try:
            content = await asyncio.to_thread(sandbox.files.read, path)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # E2B files.read() returns bytes for binary files, str for text
            if isinstance(content, bytes):
                try:
                    content = content.decode("utf-8")
                except UnicodeDecodeError:
                    content = f"[Binary file: {len(content)} bytes. Use sandbox_download to retrieve binary files.]"

            logger.info(
                "sandbox_read_file_completed",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "path": path,
                    "content_length": len(content),
                    "duration_ms": duration_ms,
                },
            )

            return {
                "path": path,
                "content": content,
            }

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "sandbox_read_file_error",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "path": path,
                    "error": str(e),
                    "duration_ms": duration_ms,
                },
            )
            return {
                "path": path,
                "content": f"Error reading file: {str(e)}",
            }

    return sandbox_read_file
```

### Step 2: Add sandbox_write_file to sandbox_tools.py

Add to `services/worker-service/tools/sandbox_tools.py` after the `sandbox_read_file` section:

```python
# --- sandbox_write_file ---

class SandboxWriteFileArguments(BaseModel):
    path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1000,
            description="Absolute or relative path where the file will be written in the sandbox.",
        ),
    ]
    content: Annotated[
        str,
        Field(
            max_length=52428800,
            description="Content to write to the file.",
        ),
    ]


class SandboxWriteFileResult(BaseModel):
    path: str
    size_bytes: int


def create_sandbox_write_file_fn(sandbox):
    """Create the sandbox_write_file async function with the sandbox bound via closure.

    Args:
        sandbox: E2B Sandbox instance

    Returns:
        Async function suitable for StructuredTool.from_function(coroutine=...)
    """

    async def sandbox_write_file(path: str, content: str) -> dict:
        start_time = time.monotonic()
        try:
            await asyncio.to_thread(sandbox.files.write, path, content)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            size_bytes = len(content.encode("utf-8"))

            logger.info(
                "sandbox_write_file_completed",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "path": path,
                    "size_bytes": size_bytes,
                    "duration_ms": duration_ms,
                },
            )

            return {
                "path": path,
                "size_bytes": size_bytes,
            }

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "sandbox_write_file_error",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "path": path,
                    "error": str(e),
                    "duration_ms": duration_ms,
                },
            )
            return {
                "path": path,
                "size_bytes": 0,
            }

    return sandbox_write_file
```

### Step 3: Add tool definitions to definitions.py

Modify `services/worker-service/tools/definitions.py` to add the file tool definitions:

```python
from tools.sandbox_tools import (
    SandboxExecArguments, SandboxExecResult,
    SandboxReadFileArguments, SandboxReadFileResult,
    SandboxWriteFileArguments, SandboxWriteFileResult,
)

SANDBOX_READ_FILE_TOOL = ToolDefinition(
    name="sandbox_read_file",
    description="Read the content of a file from the sandbox filesystem. Returns the file content as text.",
    input_model=SandboxReadFileArguments,
    output_model=SandboxReadFileResult,
)

SANDBOX_WRITE_FILE_TOOL = ToolDefinition(
    name="sandbox_write_file",
    description="Write content to a file in the sandbox filesystem. Creates the file if it does not exist, overwrites if it does.",
    input_model=SandboxWriteFileArguments,
    output_model=SandboxWriteFileResult,
)
```

### Step 4: Register file tools in graph.py _get_tools()

Modify `services/worker-service/executor/graph.py` to register the file tools. Add imports:

```python
from tools.sandbox_tools import (
    SandboxExecArguments,
    SandboxReadFileArguments,
    SandboxWriteFileArguments,
    create_sandbox_exec_fn,
    create_sandbox_read_file_fn,
    create_sandbox_write_file_fn,
)
from tools.definitions import SANDBOX_EXEC_TOOL, SANDBOX_READ_FILE_TOOL, SANDBOX_WRITE_FILE_TOOL
```

Add after the `sandbox_exec` registration block in `_get_tools()`:

```python
        if sandbox is not None and "sandbox_read_file" in allowed_tools:
            read_fn = create_sandbox_read_file_fn(sandbox)

            async def sandbox_read_file_wrapper(path: str):
                return await self._await_or_cancel(
                    read_fn(path),
                    cancel_event,
                    task_id=task_id,
                    operation="sandbox_read_file",
                )

            tools.append(StructuredTool.from_function(
                coroutine=sandbox_read_file_wrapper,
                name="sandbox_read_file",
                description=SANDBOX_READ_FILE_TOOL.description,
                args_schema=SandboxReadFileArguments,
            ))

        if sandbox is not None and "sandbox_write_file" in allowed_tools:
            write_fn = create_sandbox_write_file_fn(sandbox)

            async def sandbox_write_file_wrapper(path: str, content: str):
                return await self._await_or_cancel(
                    write_fn(path, content),
                    cancel_event,
                    task_id=task_id,
                    operation="sandbox_write_file",
                )

            tools.append(StructuredTool.from_function(
                coroutine=sandbox_write_file_wrapper,
                name="sandbox_write_file",
                description=SANDBOX_WRITE_FILE_TOOL.description,
                args_schema=SandboxWriteFileArguments,
            ))
```

### Step 5: Write unit tests

Add to `services/worker-service/tests/test_sandbox_tools.py`:

```python
from tools.sandbox_tools import (
    SandboxReadFileArguments,
    SandboxReadFileResult,
    SandboxWriteFileArguments,
    SandboxWriteFileResult,
    create_sandbox_read_file_fn,
    create_sandbox_write_file_fn,
)


class TestSandboxReadFileArguments:
    def test_valid_path(self):
        args = SandboxReadFileArguments(path="/home/user/data.csv")
        assert args.path == "/home/user/data.csv"

    def test_empty_path_rejected(self):
        with pytest.raises(Exception):
            SandboxReadFileArguments(path="")

    def test_long_path_rejected(self):
        with pytest.raises(Exception):
            SandboxReadFileArguments(path="x" * 1001)


class TestSandboxReadFileResult:
    def test_result_fields(self):
        result = SandboxReadFileResult(path="/home/user/file.txt", content="hello")
        assert result.path == "/home/user/file.txt"
        assert result.content == "hello"


class TestCreateSandboxReadFileFn:
    @pytest.mark.asyncio
    async def test_read_text_file(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        read_fn = create_sandbox_read_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = "file content here"
            result = await read_fn("/home/user/file.txt")

        assert result["path"] == "/home/user/file.txt"
        assert result["content"] == "file content here"

    @pytest.mark.asyncio
    async def test_read_binary_file_returns_message(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        read_fn = create_sandbox_read_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = b"\x89PNG\r\n\x1a\n\x00\x00"
            result = await read_fn("/home/user/image.png")

        assert "Binary file" in result["content"]
        assert "sandbox_download" in result["content"]

    @pytest.mark.asyncio
    async def test_read_bytes_utf8_decodable(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        read_fn = create_sandbox_read_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = b"utf8 text content"
            result = await read_fn("/home/user/file.txt")

        assert result["content"] == "utf8 text content"

    @pytest.mark.asyncio
    async def test_read_file_error(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        read_fn = create_sandbox_read_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = FileNotFoundError("No such file")
            result = await read_fn("/home/user/missing.txt")

        assert "Error reading file" in result["content"]
        assert "No such file" in result["content"]


class TestSandboxWriteFileArguments:
    def test_valid_write(self):
        args = SandboxWriteFileArguments(path="/home/user/output.txt", content="hello world")
        assert args.path == "/home/user/output.txt"
        assert args.content == "hello world"

    def test_empty_path_rejected(self):
        with pytest.raises(Exception):
            SandboxWriteFileArguments(path="", content="data")

    def test_long_path_rejected(self):
        with pytest.raises(Exception):
            SandboxWriteFileArguments(path="x" * 1001, content="data")


class TestSandboxWriteFileResult:
    def test_result_fields(self):
        result = SandboxWriteFileResult(path="/home/user/output.txt", size_bytes=11)
        assert result.path == "/home/user/output.txt"
        assert result.size_bytes == 11


class TestCreateSandboxWriteFileFn:
    @pytest.mark.asyncio
    async def test_write_success(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        write_fn = create_sandbox_write_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            result = await write_fn("/home/user/output.txt", "hello world")

        assert result["path"] == "/home/user/output.txt"
        assert result["size_bytes"] == len("hello world".encode("utf-8"))
        mock_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_empty_content(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        write_fn = create_sandbox_write_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock):
            result = await write_fn("/home/user/empty.txt", "")

        assert result["size_bytes"] == 0

    @pytest.mark.asyncio
    async def test_write_error(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        write_fn = create_sandbox_write_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = PermissionError("Permission denied")
            result = await write_fn("/root/protected.txt", "data")

        assert result["size_bytes"] == 0

    @pytest.mark.asyncio
    async def test_write_unicode_content(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        write_fn = create_sandbox_write_file_fn(mock_sandbox)

        unicode_content = "Hello, \u4e16\u754c! \U0001f600"

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock):
            result = await write_fn("/home/user/unicode.txt", unicode_content)

        assert result["size_bytes"] == len(unicode_content.encode("utf-8"))
```

## Acceptance Criteria

- [ ] `SandboxReadFileArguments` has `path` field: str, min_length=1, max_length=1000
- [ ] `SandboxReadFileResult` has `path` (str) and `content` (str)
- [ ] `SandboxWriteFileArguments` has `path` (str, max 1000) and `content` (str, max 52428800)
- [ ] `SandboxWriteFileResult` has `path` (str) and `size_bytes` (int)
- [ ] `create_sandbox_read_file_fn()` reads via `sandbox.files.read(path)` wrapped in `asyncio.to_thread()`
- [ ] Binary content from `files.read()` handled gracefully (UTF-8 decode or binary message)
- [ ] `create_sandbox_write_file_fn()` writes via `sandbox.files.write(path, content)` wrapped in `asyncio.to_thread()`
- [ ] `SANDBOX_READ_FILE_TOOL` and `SANDBOX_WRITE_FILE_TOOL` added to `definitions.py`
- [ ] Both tools registered in `_get_tools()` conditional on `sandbox is not None` AND tool name in `allowed_tools`
- [ ] Tool invocations go through `_await_or_cancel()` for cancellation support
- [ ] Errors return descriptive messages — never raise to the LLM
- [ ] All file operations logged at INFO level with `sandbox_id`, `path`, `duration_ms`
- [ ] All unit tests pass
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests:** Argument validation for paths. Read text file success. Read binary file returns descriptive message. Read UTF-8 bytes decoded. Read file error handled. Write success with size calculation. Write empty content. Write error handled. Write unicode content with correct byte count.
- **Regression:** `make test` — all existing tests pass.

## Constraints and Guardrails

- Do not create the sandbox — the sandbox instance is provided via closure.
- Do not modify `sandbox_exec` code — only add new tools to the same file.
- Tools must never raise exceptions to the LLM — always return a result dict.
- Do not add the `sandbox_download` tool — Task 5 handles that.
- Content size for `sandbox_write_file` is limited to 50 MB (52428800 bytes) via Pydantic field max_length.

## Assumptions

- Task 3 has been completed (`sandbox_tools.py` exists with sandbox_exec implementation).
- The E2B `Sandbox` instance has `files.read(path)` returning str or bytes, and `files.write(path, content)`.
- The `sandbox_id` attribute is available on the sandbox instance for logging.
- The `_get_tools()` method already has the `sandbox=None` parameter from Task 3.

<!-- AGENT_TASK_END: task-4-sandbox-file-tools.md -->
