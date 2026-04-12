<!-- AGENT_TASK_START: task-3-sandbox-exec-tool.md -->

# Task 3 — sandbox_exec Built-in Tool

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 1: built-in sandbox tools)
2. `docs/exec-plans/active/agent-capabilities/track-2/plan.md` — Track 2 execution plan
3. `services/worker-service/tools/definitions.py` — existing tool definition patterns (Pydantic models, ToolDefinition dataclass)
4. `services/worker-service/executor/graph.py` — `_get_tools()` tool registration pattern (lines 138-212)
5. `services/worker-service/sandbox/provisioner.py` — Task 2 output: SandboxProvisioner and Sandbox type

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-2/progress.md` to "Done".

## Context

The `sandbox_exec` tool allows the LLM agent to execute shell commands inside the E2B sandbox. It is exposed as a built-in tool (same as `web_search`, `calculator`) and is conditionally registered when the agent has `sandbox.enabled: true` AND `"sandbox_exec"` is in the agent's `allowed_tools`.

The tool receives the sandbox instance via a closure — the executor passes the sandbox reference when registering the tool. The tool function itself does not create or manage the sandbox.

## Task-Specific Shared Contract

- Tool name: `sandbox_exec`
- Arguments: `command` (str, max 10000 chars)
- Result: `stdout` (str), `stderr` (str), `exit_code` (int)
- Implementation: calls `sandbox.commands.run(command)` via `asyncio.to_thread()`
- Per-command timeout: 300 seconds (configurable)
- The sandbox instance is passed into the tool function via closure from `execute_task()`
- Registration pattern matches existing tools in `_get_tools()`: check `allowed_tools`, create async function, wrap with `StructuredTool.from_function()`
- Only registered when agent config has `sandbox.enabled: true` AND `"sandbox_exec"` is in `allowed_tools`

## Affected Component

- **Service/Module:** Worker Service — Sandbox Tools
- **File paths:**
  - `services/worker-service/tools/sandbox_tools.py` (new)
  - `services/worker-service/tools/definitions.py` (modify — add SANDBOX_EXEC_TOOL definition)
  - `services/worker-service/executor/graph.py` (modify — register sandbox_exec in `_get_tools()`)
  - `services/worker-service/tests/test_sandbox_tools.py` (new)
- **Change type:** new code + modification

## Dependencies

- **Must complete first:** Task 2 (Sandbox Provisioner — provides Sandbox type)
- **Provides output to:** Task 9 (Integration Tests)
- **Shared interfaces/contracts:** `sandbox_tools.py` module is shared with Tasks 4 and 5 (they add more tools to the same file)

## Implementation Specification

### Step 1: Create sandbox tools module

Create `services/worker-service/tools/sandbox_tools.py`:

```python
"""Built-in sandbox tools for E2B code execution environments.

Tools in this module are conditionally registered when the agent has
sandbox.enabled: true. They receive the sandbox instance via closure.
"""

import asyncio
import logging
import time
from typing import Annotated

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_COMMAND_TIMEOUT_SECONDS = 300


# --- sandbox_exec ---

class SandboxExecArguments(BaseModel):
    command: Annotated[
        str,
        Field(
            min_length=1,
            max_length=10000,
            description="Shell command to execute in the sandbox.",
        ),
    ]


class SandboxExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


def create_sandbox_exec_fn(sandbox, *, command_timeout: int = DEFAULT_COMMAND_TIMEOUT_SECONDS):
    """Create the sandbox_exec async function with the sandbox bound via closure.

    Args:
        sandbox: E2B Sandbox instance
        command_timeout: Maximum seconds per command execution

    Returns:
        Async function suitable for StructuredTool.from_function(coroutine=...)
    """

    async def sandbox_exec(command: str) -> dict:
        start_time = time.monotonic()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(sandbox.commands.run, command),
                timeout=command_timeout,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_exec_completed",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "command_length": len(command),
                    "exit_code": result.exit_code,
                    "stdout_length": len(result.stdout) if result.stdout else 0,
                    "stderr_length": len(result.stderr) if result.stderr else 0,
                    "duration_ms": duration_ms,
                },
            )

            return {
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "exit_code": result.exit_code,
            }

        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning(
                "sandbox_exec_timeout",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "command_length": len(command),
                    "timeout_seconds": command_timeout,
                    "duration_ms": duration_ms,
                },
            )
            return {
                "stdout": "",
                "stderr": f"Command timed out after {command_timeout} seconds",
                "exit_code": -1,
            }

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "sandbox_exec_error",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "command_length": len(command),
                    "error": str(e),
                    "duration_ms": duration_ms,
                },
            )
            return {
                "stdout": "",
                "stderr": f"Command execution failed: {str(e)}",
                "exit_code": -1,
            }

    return sandbox_exec
```

### Step 2: Add SANDBOX_EXEC_TOOL to definitions.py

Modify `services/worker-service/tools/definitions.py` to add the sandbox exec tool definition. Add the import and definition after the existing tool definitions:

```python
from tools.sandbox_tools import SandboxExecArguments, SandboxExecResult

SANDBOX_EXEC_TOOL = ToolDefinition(
    name="sandbox_exec",
    description="Execute a shell command in the sandbox environment. Returns stdout, stderr, and exit code.",
    input_model=SandboxExecArguments,
    output_model=SandboxExecResult,
)
```

### Step 3: Register sandbox_exec in graph.py _get_tools()

Modify `services/worker-service/executor/graph.py` to add sandbox tool registration. The `_get_tools()` method needs to accept an optional sandbox parameter and register sandbox tools when available.

Add to the imports at the top of `graph.py`:

```python
from tools.sandbox_tools import (
    SandboxExecArguments,
    create_sandbox_exec_fn,
)
from tools.definitions import SANDBOX_EXEC_TOOL
```

Modify `_get_tools()` signature and add sandbox_exec registration at the end:

```python
    def _get_tools(
        self,
        allowed_tools: list[str],
        *,
        cancel_event: asyncio.Event,
        task_id: str,
        sandbox=None,
    ) -> list[StructuredTool]:
        tools = []
        # ... existing tool registrations (web_search, read_url, calculator, etc.) ...

        # --- Sandbox tools (only when sandbox is provisioned) ---
        if sandbox is not None and "sandbox_exec" in allowed_tools:
            exec_fn = create_sandbox_exec_fn(sandbox)

            async def sandbox_exec_wrapper(command: str):
                return await self._await_or_cancel(
                    exec_fn(command),
                    cancel_event,
                    task_id=task_id,
                    operation="sandbox_exec",
                )

            tools.append(StructuredTool.from_function(
                coroutine=sandbox_exec_wrapper,
                name="sandbox_exec",
                description=SANDBOX_EXEC_TOOL.description,
                args_schema=SandboxExecArguments,
            ))

        return tools
```

### Step 4: Write unit tests

Create `services/worker-service/tests/test_sandbox_tools.py`:

```python
"""Unit tests for sandbox tools."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.sandbox_tools import (
    SandboxExecArguments,
    SandboxExecResult,
    create_sandbox_exec_fn,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
)


class TestSandboxExecArguments:
    def test_valid_command(self):
        args = SandboxExecArguments(command="echo hello")
        assert args.command == "echo hello"

    def test_empty_command_rejected(self):
        with pytest.raises(Exception):
            SandboxExecArguments(command="")

    def test_long_command_rejected(self):
        with pytest.raises(Exception):
            SandboxExecArguments(command="x" * 10001)

    def test_max_length_command_accepted(self):
        args = SandboxExecArguments(command="x" * 10000)
        assert len(args.command) == 10000


class TestSandboxExecResult:
    def test_result_fields(self):
        result = SandboxExecResult(stdout="hello\n", stderr="", exit_code=0)
        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.exit_code == 0


class TestCreateSandboxExecFn:
    @pytest.mark.asyncio
    async def test_exec_success(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"
        mock_result = MagicMock()
        mock_result.stdout = "hello world"
        mock_result.stderr = ""
        mock_result.exit_code = 0
        mock_sandbox.commands.run = MagicMock(return_value=mock_result)

        exec_fn = create_sandbox_exec_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_result
            result = await exec_fn("echo hello world")

        assert result["stdout"] == "hello world"
        assert result["stderr"] == ""
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_exec_with_stderr(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "command not found"
        mock_result.exit_code = 127
        mock_sandbox.commands.run = MagicMock(return_value=mock_result)

        exec_fn = create_sandbox_exec_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_result
            result = await exec_fn("nonexistent-command")

        assert result["stderr"] == "command not found"
        assert result["exit_code"] == 127

    @pytest.mark.asyncio
    async def test_exec_timeout(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        exec_fn = create_sandbox_exec_fn(mock_sandbox, command_timeout=1)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = asyncio.TimeoutError()
            result = await exec_fn("sleep 999")

        assert result["exit_code"] == -1
        assert "timed out" in result["stderr"]

    @pytest.mark.asyncio
    async def test_exec_error(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        exec_fn = create_sandbox_exec_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = RuntimeError("Sandbox connection lost")
            result = await exec_fn("echo test")

        assert result["exit_code"] == -1
        assert "Sandbox connection lost" in result["stderr"]

    @pytest.mark.asyncio
    async def test_exec_null_stdout_stderr(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"
        mock_result = MagicMock()
        mock_result.stdout = None
        mock_result.stderr = None
        mock_result.exit_code = 0

        exec_fn = create_sandbox_exec_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_result
            result = await exec_fn("true")

        assert result["stdout"] == ""
        assert result["stderr"] == ""
        assert result["exit_code"] == 0

    def test_default_timeout(self):
        assert DEFAULT_COMMAND_TIMEOUT_SECONDS == 300
```

## Acceptance Criteria

- [ ] `tools/sandbox_tools.py` exists with `SandboxExecArguments`, `SandboxExecResult`, and `create_sandbox_exec_fn()`
- [ ] `SandboxExecArguments` has `command` field: str, min_length=1, max_length=10000
- [ ] `SandboxExecResult` has `stdout` (str), `stderr` (str), `exit_code` (int)
- [ ] `create_sandbox_exec_fn()` takes a sandbox instance and returns an async function
- [ ] The exec function calls `sandbox.commands.run(command)` via `asyncio.to_thread()`
- [ ] Per-command timeout of 300 seconds; returns exit_code=-1 on timeout
- [ ] Errors return exit_code=-1 with error message in stderr (never raises to the LLM)
- [ ] `SANDBOX_EXEC_TOOL` added to `definitions.py`
- [ ] `sandbox_exec` registered in `_get_tools()` only when `sandbox is not None` AND `"sandbox_exec"` in `allowed_tools`
- [ ] Tool invocations go through `_await_or_cancel()` for cancellation support
- [ ] `_get_tools()` signature extended with optional `sandbox` parameter
- [ ] All sandbox tool calls logged at INFO level with `sandbox_id`, `duration_ms`, `exit_code`
- [ ] All unit tests pass
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests:** Argument validation (valid, empty, too long). Result model fields. Exec success with stdout/stderr. Exec with non-zero exit code. Exec timeout returns error result. Exec exception returns error result. Null stdout/stderr handled as empty strings.
- **Regression:** `make test` — all existing executor and tool tests pass.

## Constraints and Guardrails

- Do not create the sandbox or manage its lifecycle — the sandbox instance is provided via closure.
- Do not modify the executor's `execute_task()` — Task 7 handles sandbox provisioning integration.
- The tool must never raise exceptions to the LLM — always return a result dict with exit_code indicating success/failure.
- Do not add file I/O tools to this file yet — Tasks 4 and 5 will add them.
- Keep `_get_tools()` backward compatible — the `sandbox` parameter must be optional with default `None`.

## Assumptions

- Task 2 has been completed (`sandbox/provisioner.py` exists, providing the `Sandbox` type).
- The E2B `Sandbox` instance has a `commands.run(command)` method that returns an object with `stdout` (str|None), `stderr` (str|None), and `exit_code` (int) attributes.
- The E2B `Sandbox` instance has a `sandbox_id` attribute for logging.
- `_get_tools()` is called from `_build_graph()` which will need to pass the sandbox instance — this integration is handled by Task 7.
- The `_await_or_cancel()` method is available on `GraphExecutor` for cancellation-aware tool invocation.

<!-- AGENT_TASK_END: task-3-sandbox-exec-tool.md -->
