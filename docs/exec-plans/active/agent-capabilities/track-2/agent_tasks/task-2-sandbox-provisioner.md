<!-- AGENT_TASK_START: task-2-sandbox-provisioner.md -->

# Task 2 — E2B SDK Setup + Sandbox Provisioner + Lifecycle

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 1: sandbox lifecycle, crash recovery, HITL pausing)
2. `docs/exec-plans/active/agent-capabilities/track-2/plan.md` — Track 2 execution plan
3. `services/worker-service/pyproject.toml` — current dependency list
4. `services/worker-service/executor/graph.py` — `execute_task()` entry point where sandbox will be used (for future context)

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-2/progress.md` to "Done".

## Context

The sandbox provisioner manages the E2B sandbox lifecycle — creating, connecting, pausing, resuming, and destroying sandboxes. It wraps the E2B SDK and provides an async-friendly interface that the executor and sandbox tools will use.

All E2B SDK methods are synchronous. This module wraps them with `asyncio.to_thread()` to avoid blocking the event loop.

The provisioner does NOT integrate with the executor or register tools — those are separate tasks (Tasks 3-7). This task delivers a standalone, unit-tested module.

## Task-Specific Shared Contract

- `SandboxProvisioner` is the single interface for all E2B SDK interactions. Sandbox tools (Tasks 3-5) and the executor (Tasks 6-7) receive a sandbox instance from the provisioner — they never call the E2B SDK directly.
- `provision()` creates a new sandbox and returns the E2B `Sandbox` instance.
- `connect()` reconnects to an existing sandbox by ID (for crash recovery).
- `pause()` pauses a sandbox (stops billing) for HITL waits.
- `resume()` resumes a paused sandbox.
- `destroy()` kills a sandbox.
- E2B API key comes from `E2B_API_KEY` environment variable.
- Retry logic: 3 attempts with exponential backoff (1s, 2s, 4s) on provision failures.
- All E2B SDK calls are sync — wrap with `asyncio.to_thread()`.

## Affected Component

- **Service/Module:** Worker Service — Sandbox Provisioner
- **File paths:**
  - `services/worker-service/pyproject.toml` (modify — add `e2b-code-interpreter` dependency)
  - `services/worker-service/sandbox/__init__.py` (new)
  - `services/worker-service/sandbox/provisioner.py` (new)
  - `services/worker-service/tests/test_sandbox_provisioner.py` (new)
- **Change type:** new code + dependency addition

## Dependencies

- **Must complete first:** Task 1 (DB Migration — `sandbox_id` column exists for crash recovery context)
- **Provides output to:** Task 3 (sandbox_exec), Task 4 (sandbox file tools), Task 5 (sandbox_download), Task 6 (Multipart + Injection), Task 7 (Crash Recovery + Cost)
- **Shared interfaces/contracts:** `SandboxProvisioner` class API; sandbox instance type

## Implementation Specification

### Step 1: Add e2b-code-interpreter dependency

Modify `services/worker-service/pyproject.toml` to add the E2B SDK:

```toml
dependencies = [
    "asyncpg>=0.29.0",
    "beautifulsoup4>=4.13.0",
    "e2b-code-interpreter>=1.2.0",
    "httpx>=0.28.0",
    "langchain>=0.3.0",
    "langchain-core>=0.3.0",
    "langchain-anthropic>=0.3.0",
    "langchain-openai>=0.3.0",
    "langchain-aws>=0.2.0",
    "langfuse==4.0.1",
    "langgraph==1.0.5",
    "langgraph-prebuilt==1.0.8",
    "langgraph-checkpoint==3.0.1",
    "langgraph-checkpoint-postgres==3.0.4",
    "mcp==1.26.0",
    "psycopg[binary]>=3.1.0",
    "pydantic>=2.11.0",
    "python-dotenv>=1.0.0",
    "structlog>=24.1.0",
]
```

After modifying `pyproject.toml`, install the new dependency:
```bash
cd services/worker-service && .venv/bin/pip install -e ".[dev]"
```

### Step 2: Create sandbox package init

Create `services/worker-service/sandbox/__init__.py`:

```python
"""E2B sandbox management for code execution environments."""
```

### Step 3: Create sandbox provisioner

Create `services/worker-service/sandbox/provisioner.py`:

```python
"""E2B sandbox provisioner — lifecycle management for code execution environments."""

import asyncio
import logging
import os
import time

from e2b_code_interpreter import Sandbox

logger = logging.getLogger(__name__)

DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = [1, 2, 4]


class SandboxProvisionError(Exception):
    """Raised when sandbox provisioning fails after all retries."""

    def __init__(self, template: str, message: str):
        self.template = template
        super().__init__(f"Failed to provision sandbox with template '{template}': {message}")


class SandboxConnectionError(Exception):
    """Raised when reconnecting to an existing sandbox fails."""

    def __init__(self, sandbox_id: str, message: str):
        self.sandbox_id = sandbox_id
        super().__init__(f"Failed to connect to sandbox '{sandbox_id}': {message}")


class SandboxProvisioner:
    """Manages E2B sandbox lifecycle: provision, connect, pause, resume, destroy.

    All E2B SDK methods are synchronous. This class wraps them with
    asyncio.to_thread() to avoid blocking the event loop.

    Usage:
        provisioner = SandboxProvisioner()
        sandbox = await provisioner.provision("python-3.11", vcpu=2, memory_mb=2048, timeout_seconds=3600)
        sandbox_id = sandbox.sandbox_id
        # ... use sandbox ...
        await provisioner.destroy(sandbox)
    """

    def __init__(self, api_key: str | None = None):
        """Initialize the provisioner.

        Args:
            api_key: E2B API key. If None, reads from E2B_API_KEY env var.
        """
        self._api_key = api_key or os.environ.get("E2B_API_KEY")
        if not self._api_key:
            raise ValueError("E2B API key not provided and E2B_API_KEY env var not set")

    async def provision(
        self,
        template: str,
        vcpu: int = 2,
        memory_mb: int = 2048,
        timeout_seconds: int = 3600,
    ) -> Sandbox:
        """Provision a new E2B sandbox.

        Retries up to 3 times with exponential backoff (1s, 2s, 4s) on failure.

        Args:
            template: E2B sandbox template (e.g., "python-3.11")
            vcpu: CPU allocation (1-8). Stored in agent config for future use /
                custom template selection, but NOT passed to the current E2B SDK.
                E2B resource allocation is template-based.
            memory_mb: Memory allocation in MB (512-8192). Stored in agent config
                for future use / custom template selection, but NOT passed to the
                current E2B SDK. E2B resource allocation is template-based.
            timeout_seconds: Maximum sandbox lifetime in seconds (60-86400)

        Returns:
            E2B Sandbox instance

        Raises:
            SandboxProvisionError: if provisioning fails after all retries

        Note:
            ``vcpu`` and ``memory_mb`` are accepted for forward compatibility but
            are not sent to E2B. The E2B ``e2b-code-interpreter`` SDK controls
            resources via the template. These parameters are validated in Task 1's
            agent config and may be used for custom template selection in the future.
        """
        last_error: Exception | None = None

        for attempt in range(DEFAULT_RETRY_ATTEMPTS):
            try:
                start_time = time.monotonic()
                sandbox = await asyncio.to_thread(
                    Sandbox.create,
                    template=template,
                    cwd="/home/user",
                    api_key=self._api_key,
                    timeout=timeout_seconds,
                )
                duration_ms = int((time.monotonic() - start_time) * 1000)

                logger.info(
                    "sandbox_provisioned",
                    extra={
                        "sandbox_id": sandbox.sandbox_id,
                        "template": template,
                        "vcpu": vcpu,
                        "memory_mb": memory_mb,
                        "timeout_seconds": timeout_seconds,
                        "duration_ms": duration_ms,
                        "attempt": attempt + 1,
                    },
                )
                return sandbox

            except Exception as e:
                last_error = e
                if attempt < DEFAULT_RETRY_ATTEMPTS - 1:
                    backoff = DEFAULT_BACKOFF_SECONDS[attempt]
                    logger.warning(
                        "sandbox_provision_retry",
                        extra={
                            "template": template,
                            "attempt": attempt + 1,
                            "backoff_seconds": backoff,
                            "error": str(e),
                        },
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "sandbox_provision_failed",
                        extra={
                            "template": template,
                            "attempts": DEFAULT_RETRY_ATTEMPTS,
                            "error": str(e),
                        },
                    )

        raise SandboxProvisionError(template, str(last_error))

    async def connect(self, sandbox_id: str) -> Sandbox:
        """Reconnect to an existing sandbox by ID.

        Used for crash recovery — the worker reads sandbox_id from the DB
        and reconnects to continue execution.

        Args:
            sandbox_id: E2B sandbox ID from a previous provision() call

        Returns:
            E2B Sandbox instance

        Raises:
            SandboxConnectionError: if the sandbox cannot be reached (expired, etc.)
        """
        try:
            start_time = time.monotonic()
            sandbox = await asyncio.to_thread(
                Sandbox.connect,
                sandbox_id,
                api_key=self._api_key,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_reconnected",
                extra={
                    "sandbox_id": sandbox_id,
                    "duration_ms": duration_ms,
                },
            )
            return sandbox

        except Exception as e:
            logger.error(
                "sandbox_reconnect_failed",
                extra={
                    "sandbox_id": sandbox_id,
                    "error": str(e),
                },
            )
            raise SandboxConnectionError(sandbox_id, str(e)) from e

    async def pause(self, sandbox: Sandbox) -> None:
        """Pause a sandbox (stops billing).

        Used when a task enters HITL waiting state. The sandbox filesystem
        is preserved but compute is stopped.

        Args:
            sandbox: E2B Sandbox instance to pause
        """
        sandbox_id = sandbox.sandbox_id
        try:
            start_time = time.monotonic()
            await asyncio.to_thread(sandbox.pause)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_paused",
                extra={
                    "sandbox_id": sandbox_id,
                    "duration_ms": duration_ms,
                },
            )
        except Exception as e:
            logger.warning(
                "sandbox_pause_failed",
                extra={
                    "sandbox_id": sandbox_id,
                    "error": str(e),
                },
            )
            # Don't raise — sandbox timeout will handle cleanup if pause fails

    async def resume(self, sandbox_id: str) -> Sandbox:
        """Resume a paused sandbox.

        Used when a task resumes from HITL waiting state. E2B auto-resumes
        paused sandboxes on connect.

        Args:
            sandbox_id: E2B sandbox ID of the paused sandbox

        Returns:
            E2B Sandbox instance

        Raises:
            SandboxConnectionError: if the sandbox cannot be resumed
        """
        try:
            start_time = time.monotonic()
            sandbox = await asyncio.to_thread(
                Sandbox.connect,
                sandbox_id,
                api_key=self._api_key,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_resumed",
                extra={
                    "sandbox_id": sandbox_id,
                    "duration_ms": duration_ms,
                },
            )
            return sandbox

        except Exception as e:
            logger.error(
                "sandbox_resume_failed",
                extra={
                    "sandbox_id": sandbox_id,
                    "error": str(e),
                },
            )
            raise SandboxConnectionError(sandbox_id, str(e)) from e

    async def destroy(self, sandbox: Sandbox) -> None:
        """Destroy a sandbox and release all resources.

        Called on task completion. Best-effort — if destroy fails, E2B
        will auto-expire the sandbox based on its timeout.

        Args:
            sandbox: E2B Sandbox instance to destroy
        """
        sandbox_id = sandbox.sandbox_id
        try:
            start_time = time.monotonic()
            await asyncio.to_thread(sandbox.kill)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_destroyed",
                extra={
                    "sandbox_id": sandbox_id,
                    "duration_ms": duration_ms,
                },
            )
        except Exception as e:
            logger.warning(
                "sandbox_destroy_failed",
                extra={
                    "sandbox_id": sandbox_id,
                    "error": str(e),
                },
            )
            # Don't raise — E2B auto-expires sandboxes if destroy fails
```

### Step 4: Write unit tests

Create `services/worker-service/tests/test_sandbox_provisioner.py`:

```python
"""Unit tests for SandboxProvisioner."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sandbox.provisioner import (
    SandboxConnectionError,
    SandboxProvisionError,
    SandboxProvisioner,
)


@pytest.fixture
def provisioner():
    return SandboxProvisioner(api_key="test-api-key")


class TestSandboxProvisionerInit:
    def test_init_with_explicit_key(self):
        p = SandboxProvisioner(api_key="my-key")
        assert p._api_key == "my-key"

    def test_init_with_env_var(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "env-key")
        p = SandboxProvisioner()
        assert p._api_key == "env-key"

    def test_init_no_key_raises(self, monkeypatch):
        monkeypatch.delenv("E2B_API_KEY", raising=False)
        with pytest.raises(ValueError, match="E2B API key"):
            SandboxProvisioner()


class TestSandboxProvisionerProvision:
    @pytest.mark.asyncio
    async def test_provision_success(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-123"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_sandbox
            result = await provisioner.provision("python-3.11", vcpu=2, memory_mb=2048, timeout_seconds=3600)

        assert result == mock_sandbox
        mock_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_provision_retries_on_failure(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-456"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = [
                ConnectionError("E2B API down"),
                ConnectionError("E2B API still down"),
                mock_sandbox,
            ]
            with patch("sandbox.provisioner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await provisioner.provision("python-3.11")

        assert result == mock_sandbox
        assert mock_thread.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @pytest.mark.asyncio
    async def test_provision_exhausts_retries_raises(self, provisioner):
        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = ConnectionError("E2B API down")
            with patch("sandbox.provisioner.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(SandboxProvisionError, match="python-3.11"):
                    await provisioner.provision("python-3.11")

        assert mock_thread.call_count == 3


class TestSandboxProvisionerConnect:
    @pytest.mark.asyncio
    async def test_connect_success(self, provisioner):
        mock_sandbox = MagicMock()

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_sandbox
            result = await provisioner.connect("sbx-existing-123")

        assert result == mock_sandbox

    @pytest.mark.asyncio
    async def test_connect_failure_raises(self, provisioner):
        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = Exception("Sandbox not found")
            with pytest.raises(SandboxConnectionError, match="sbx-expired"):
                await provisioner.connect("sbx-expired")


class TestSandboxProvisionerPause:
    @pytest.mark.asyncio
    async def test_pause_success(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-pause-123"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock):
            await provisioner.pause(mock_sandbox)
            # Should not raise

    @pytest.mark.asyncio
    async def test_pause_failure_does_not_raise(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-pause-fail"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = Exception("Pause failed")
            # Should NOT raise — pause failure is logged but swallowed
            await provisioner.pause(mock_sandbox)


class TestSandboxProvisionerResume:
    @pytest.mark.asyncio
    async def test_resume_success(self, provisioner):
        mock_sandbox = MagicMock()

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_sandbox
            result = await provisioner.resume("sbx-paused-123")

        assert result == mock_sandbox

    @pytest.mark.asyncio
    async def test_resume_failure_raises(self, provisioner):
        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = Exception("Sandbox expired during pause")
            with pytest.raises(SandboxConnectionError, match="sbx-expired"):
                await provisioner.resume("sbx-expired")


class TestSandboxProvisionerDestroy:
    @pytest.mark.asyncio
    async def test_destroy_success(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-destroy-123"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock):
            await provisioner.destroy(mock_sandbox)
            # Should not raise

    @pytest.mark.asyncio
    async def test_destroy_failure_does_not_raise(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-destroy-fail"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = Exception("Destroy failed")
            # Should NOT raise — destroy failure is logged but swallowed
            await provisioner.destroy(mock_sandbox)


class TestSandboxProvisionError:
    def test_error_message(self):
        err = SandboxProvisionError("python-3.11", "API timeout")
        assert "python-3.11" in str(err)
        assert "API timeout" in str(err)
        assert err.template == "python-3.11"


class TestSandboxConnectionError:
    def test_error_message(self):
        err = SandboxConnectionError("sbx-abc", "not found")
        assert "sbx-abc" in str(err)
        assert "not found" in str(err)
        assert err.sandbox_id == "sbx-abc"
```

## Acceptance Criteria

- [ ] `e2b-code-interpreter>=1.2.0` is added to `pyproject.toml` dependencies
- [ ] `sandbox/__init__.py` exists
- [ ] `sandbox/provisioner.py` exists with `SandboxProvisioner` class
- [ ] `SandboxProvisioner.__init__()` accepts optional `api_key` or reads from `E2B_API_KEY` env var
- [ ] `provision()` creates a sandbox via `Sandbox.create()` class method, wrapping sync call with `asyncio.to_thread()`
- [ ] `provision()` does NOT pass `vcpu` or `memory_mb` to the E2B SDK (E2B resource allocation is template-based); these params are stored for future use
- [ ] `provision()` retries 3 times with exponential backoff (1s, 2s, 4s) on failure
- [ ] `provision()` raises `SandboxProvisionError` after exhausting retries
- [ ] `connect()` reconnects to an existing sandbox by ID
- [ ] `connect()` raises `SandboxConnectionError` on failure
- [ ] `pause()` pauses the sandbox; failure is logged but not raised
- [ ] `resume()` reconnects to a paused sandbox (auto-resume via E2B)
- [ ] `resume()` raises `SandboxConnectionError` on failure
- [ ] `destroy()` kills the sandbox; failure is logged but not raised
- [ ] All sandbox lifecycle events logged at INFO level with `sandbox_id` and `duration_ms`
- [ ] Error events logged at ERROR/WARNING level with context
- [ ] All unit tests pass with mocked E2B SDK
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests:** Init with explicit key, env var, and missing key. Provision success, retry on failure, exhaust retries. Connect success and failure. Pause success and failure (swallowed). Resume success and failure. Destroy success and failure (swallowed). Error class message formatting.
- **Manual verification:** With a real `E2B_API_KEY`, provision a sandbox with template `base`, verify it returns a sandbox with a valid `sandbox_id`, then destroy it.

## Constraints and Guardrails

- Do not integrate with the executor or graph — this task delivers a standalone module.
- Do not register sandbox tools — Tasks 3-5 handle that.
- Do not modify `executor/graph.py` — Task 7 handles executor integration.
- Do not add database queries — the provisioner is database-agnostic.
- All E2B SDK calls must go through `asyncio.to_thread()` — never call sync SDK methods directly in async code.
- Do not store or log the E2B API key value.

## Assumptions

- The `e2b-code-interpreter` package provides a `Sandbox` class with `sandbox_id` attribute, `pause()`, `kill()`, and class methods `create()`, `connect()`.
- `Sandbox.create(template=..., cwd=..., api_key=..., timeout=...)` creates a new sandbox synchronously. Note: use the `Sandbox.create()` class method, NOT the `Sandbox(...)` constructor.
- `Sandbox.connect(sandbox_id, api_key=...)` reconnects to an existing sandbox synchronously.
- `sandbox.pause()` pauses the sandbox synchronously.
- `sandbox.kill()` destroys the sandbox synchronously.
- The worker virtualenv is at `services/worker-service/.venv/`.

<!-- AGENT_TASK_END: task-2-sandbox-provisioner.md -->
