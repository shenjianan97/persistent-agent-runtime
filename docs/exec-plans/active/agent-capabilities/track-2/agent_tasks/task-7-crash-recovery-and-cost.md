<!-- AGENT_TASK_START: task-7-crash-recovery-and-cost.md -->

# Task 7 — Crash Recovery + Sandbox Cost Tracking

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 1: crash recovery, HITL pausing, sandbox cost)
2. `docs/exec-plans/active/agent-capabilities/track-2/plan.md` — Track 2 execution plan
3. `services/worker-service/executor/graph.py` — full `execute_task()` implementation (lines 406-800+), including MCP session lifecycle, HITL handling, budget enforcement, and finally block
4. `services/worker-service/sandbox/provisioner.py` — Task 2 output: SandboxProvisioner class
5. `services/worker-service/tools/sandbox_tools.py` — Tasks 3-5 output: sandbox tool factory functions
6. `infrastructure/database/migrations/0010_sandbox_support.sql` — Task 1: sandbox_id column

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-2/progress.md` to "Done".

## Context

This is the integration task that ties all sandbox components together in the executor. It adds:

1. **Sandbox provisioning** in `execute_task()` — provision or reconnect at task start
2. **Sandbox ID persistence** — store sandbox_id in DB immediately after provisioning
3. **Crash recovery** — reconnect to existing sandbox on task resume
4. **HITL sandbox pausing** — pause sandbox when task enters HITL wait, resume on return
5. **Sandbox destruction** — destroy sandbox on task completion
6. **Cost tracking** — calculate and record sandbox compute costs
7. **Provision failure handling** — dead-letter with `sandbox_provision_failed`
8. **Input file injection** — wire up Task 6's injection methods after sandbox provisioning

## Task-Specific Shared Contract

- Sandbox provisioning happens in `execute_task()` after task setup, before the LLM loop
- `sandbox_id` written to DB immediately after provisioning: `UPDATE tasks SET sandbox_id = ? WHERE task_id = ?`
- `sandbox_id` cleared on task completion: `UPDATE tasks SET sandbox_id = NULL WHERE task_id = ?`
- Crash recovery: if `task_data["sandbox_id"]` exists, attempt `provisioner.connect(sandbox_id)`
- If connect fails → dead-letter with reason `sandbox_lost`
- If provision fails after 3 retries → dead-letter with reason `sandbox_provision_failed`
- HITL pause: call `provisioner.pause(sandbox)` before releasing lease
- HITL resume: call `provisioner.resume(sandbox_id)` when task resumes
- Sandbox cost: `duration_seconds * vcpu * $0.05/3600`, added to `cost_microdollars`
- Sandbox destroyed in `finally` block (best-effort)
- `_get_tools()` called with sandbox instance and s3_client when sandbox is provisioned

## Affected Component

- **Service/Module:** Worker Service — Executor
- **File paths:**
  - `services/worker-service/executor/graph.py` (modify — sandbox lifecycle in execute_task)
  - `services/worker-service/tests/test_executor.py` (modify — add sandbox lifecycle tests)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 1 (DB migration), Task 2 (SandboxProvisioner), Task 3 (sandbox_exec), Task 4 (file tools), Task 5 (sandbox_download), Task 6 (input file injection methods)
- **Provides output to:** Task 9 (Integration Tests)
- **Shared interfaces/contracts:** SandboxProvisioner, sandbox tools, S3Client, inject_input_files

## Implementation Specification

### Step 1: Add sandbox imports to graph.py

Add to the imports at the top of `services/worker-service/executor/graph.py`:

```python
from sandbox.provisioner import (
    SandboxProvisioner,
    SandboxProvisionError,
    SandboxConnectionError,
)
```

### Step 2: Add SandboxProvisioner to GraphExecutor initialization

The `GraphExecutor.__init__()` currently accepts `config: WorkerConfig, pool: asyncpg.Pool`. Add optional `s3_client` and lazy-initialized `sandbox_provisioner`:

```python
    def __init__(self, config: WorkerConfig, pool: asyncpg.Pool, deps=None, s3_client=None):
        self.config = config
        self.pool = pool
        self.deps = deps or create_default_dependencies()
        self.s3_client = s3_client
        self._cost_rate_cache: dict[str, dict] = {}
        self._sandbox_provisioner: SandboxProvisioner | None = None

    @property
    def sandbox_provisioner(self) -> SandboxProvisioner | None:
        """Lazy-initialize the sandbox provisioner (requires E2B_API_KEY)."""
        if self._sandbox_provisioner is None:
            import os
            api_key = os.environ.get("E2B_API_KEY")
            if api_key:
                self._sandbox_provisioner = SandboxProvisioner(api_key=api_key)
        return self._sandbox_provisioner
```

### Step 3: Add sandbox lifecycle to execute_task()

Modify `execute_task()` to add sandbox provisioning after the existing setup code and before `_build_graph()`. Insert the following block after the MCP tool server setup and before the checkpointer initialization:

```python
        # --- Sandbox provisioning ---
        sandbox_config = agent_config.get("sandbox", {})
        sandbox_enabled = sandbox_config.get("enabled", False)
        sandbox = None
        sandbox_start_time = None

        if sandbox_enabled:
            provisioner = self.sandbox_provisioner
            if provisioner is None:
                logger.error(
                    "sandbox_provisioner_unavailable",
                    extra={"task_id": task_id},
                )
                await self._handle_dead_letter(
                    task_id, tenant_id, agent_id,
                    reason="sandbox_provision_failed",
                    error_msg="E2B_API_KEY not configured. Cannot provision sandbox.",
                    error_code="sandbox_provision_failed",
                )
                return

            existing_sandbox_id = task_data.get("sandbox_id")

            if existing_sandbox_id:
                # Crash recovery: reconnect to existing sandbox
                try:
                    sandbox = await provisioner.connect(existing_sandbox_id)
                    logger.info(
                        "sandbox_crash_recovery_success",
                        extra={
                            "task_id": task_id,
                            "sandbox_id": existing_sandbox_id,
                        },
                    )
                except SandboxConnectionError as e:
                    logger.warning(
                        "sandbox_crash_recovery_failed",
                        extra={
                            "task_id": task_id,
                            "sandbox_id": existing_sandbox_id,
                            "error": str(e),
                        },
                    )
                    await self._handle_dead_letter(
                        task_id, tenant_id, agent_id,
                        reason="sandbox_lost",
                        error_msg=f"Sandbox '{existing_sandbox_id}' is no longer available: {str(e)}",
                        error_code="sandbox_lost",
                    )
                    return
            else:
                # Fresh provision
                template = sandbox_config.get("template", "base")
                vcpu = sandbox_config.get("vcpu", 2)
                memory_mb = sandbox_config.get("memory_mb", 2048)
                timeout_seconds = sandbox_config.get("timeout_seconds", 3600)

                try:
                    sandbox = await provisioner.provision(
                        template=template,
                        vcpu=vcpu,
                        memory_mb=memory_mb,
                        timeout_seconds=timeout_seconds,
                    )
                except SandboxProvisionError as e:
                    logger.error(
                        "sandbox_provision_exhausted",
                        extra={
                            "task_id": task_id,
                            "template": template,
                            "error": str(e),
                        },
                    )
                    await self._handle_dead_letter(
                        task_id, tenant_id, agent_id,
                        reason="sandbox_provision_failed",
                        error_msg=str(e),
                        error_code="sandbox_provision_failed",
                    )
                    return

                # Store sandbox_id in DB immediately after provisioning
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE tasks SET sandbox_id = $1 WHERE task_id = $2::uuid",
                        sandbox.sandbox_id,
                        task_id,
                    )

                logger.info(
                    "sandbox_id_persisted",
                    extra={
                        "task_id": task_id,
                        "sandbox_id": sandbox.sandbox_id,
                    },
                )

            sandbox_start_time = time.monotonic()

            # Inject input files into sandbox
            injected_files = await self._inject_input_files(sandbox, task_id, tenant_id)
```

### Step 4: Pass sandbox to _get_tools() and _build_graph()

Modify the `_build_graph()` call to pass the sandbox through to `_get_tools()`. In the `_build_graph()` method, update the `_get_tools()` call:

```python
    async def _build_graph(
        self,
        agent_config: dict[str, Any],
        *,
        cancel_event: asyncio.Event,
        task_id: str,
        custom_tools: list[StructuredTool] | None = None,
        sandbox=None,
        s3_client=None,
        tenant_id: str = "",
        injected_files: list[str] | None = None,
    ) -> StateGraph:
```

Inside `_build_graph()`, pass sandbox and s3_client to `_get_tools()`:

```python
        tools = self._get_tools(
            allowed_tools,
            cancel_event=cancel_event,
            task_id=task_id,
            sandbox=sandbox,
            s3_client=s3_client,
            tenant_id=tenant_id,
        )
```

And if `injected_files` is not empty, prepend an input files system message:

```python
        if injected_files:
            input_files_msg = self._build_input_files_system_message(injected_files)
            if system_prompt:
                system_prompt = system_prompt + "\n\n" + input_files_msg
            else:
                system_prompt = input_files_msg
```

Update the graph build call in `execute_task()`:

```python
            graph = await self._build_graph(
                agent_config,
                cancel_event=cancel_event,
                task_id=task_id,
                custom_tools=custom_tools if custom_tools else None,
                sandbox=sandbox,
                s3_client=self.s3_client,
                tenant_id=tenant_id,
                injected_files=injected_files if sandbox_enabled else None,
            )
```

### Step 5: Add sandbox pause on HITL

In the existing HITL interrupt handling code (inside `run_astream()`), add sandbox pausing before returning. Find the section where `_handle_interrupt_from_state()` is called and the MCP session close block:

```python
                            await self._handle_interrupt_from_state(task_data, interrupt_data, worker_id, original_tool_prompt=original_tool_prompt)
                            # Close MCP sessions before releasing lease on HITL pause
                            if session_manager is not None:
                                await session_manager.close("paused")
                                session_manager = None
                            # Pause sandbox before releasing lease on HITL pause
                            if sandbox is not None:
                                await provisioner.pause(sandbox)
                                sandbox = None  # Prevent double-destroy in finally
                            return
```

Similarly, in the budget pause section where MCP sessions are closed:

```python
                                                    if was_paused:
                                                        if session_manager is not None:
                                                            await session_manager.close("paused")
                                                            session_manager = None
                                                        # Pause sandbox before releasing lease on budget pause
                                                        if sandbox is not None:
                                                            await provisioner.pause(sandbox)
                                                            sandbox = None
                                                        return
```

### Step 6: Add sandbox cleanup and cost tracking to completion and finally

In the task completion section (after final output is computed, before the UPDATE to `completed`), add sandbox cost tracking and destruction:

```python
                # Sandbox cleanup and cost tracking
                sandbox_cost_microdollars = 0
                if sandbox is not None and sandbox_start_time is not None:
                    sandbox_duration_seconds = time.monotonic() - sandbox_start_time
                    sandbox_vcpu = sandbox_config.get("vcpu", 2)
                    # E2B cost: $0.05/hour per vCPU, per-second billing
                    sandbox_cost_microdollars = int(
                        sandbox_duration_seconds * sandbox_vcpu * 50000 / 3600
                    )

                    logger.info(
                        "sandbox_cost_calculated",
                        extra={
                            "task_id": task_id,
                            "sandbox_id": sandbox.sandbox_id,
                            "duration_seconds": round(sandbox_duration_seconds, 1),
                            "vcpu": sandbox_vcpu,
                            "cost_microdollars": sandbox_cost_microdollars,
                        },
                    )

                    # Add sandbox cost to the task's cost via the cost ledger
                    if sandbox_cost_microdollars > 0:
                        try:
                            async with self.pool.acquire() as cost_conn:
                                await cost_conn.execute(
                                    """INSERT INTO agent_cost_ledger
                                       (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
                                       VALUES ($1, $2, $3::uuid, 'sandbox', $4)""",
                                    tenant_id,
                                    agent_id,
                                    task_id,
                                    sandbox_cost_microdollars,
                                )
                        except Exception:
                            logger.warning(
                                "sandbox_cost_recording_failed",
                                extra={"task_id": task_id},
                                exc_info=True,
                            )

                    # Destroy sandbox
                    try:
                        await provisioner.destroy(sandbox)
                    except Exception:
                        logger.warning(
                            "sandbox_destroy_on_completion_failed",
                            extra={"task_id": task_id},
                            exc_info=True,
                        )

                    # Clear sandbox_id in DB
                    try:
                        async with self.pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE tasks SET sandbox_id = NULL WHERE task_id = $1::uuid",
                                task_id,
                            )
                    except Exception:
                        logger.warning(
                            "sandbox_id_clear_failed",
                            extra={"task_id": task_id},
                            exc_info=True,
                        )

                    sandbox = None  # Prevent double-destroy in finally
```

In the `finally` block, add sandbox cleanup after the MCP session close:

```python
        finally:
            if per_task_langfuse_client is not None:
                try:
                    per_task_langfuse_client.flush()
                except Exception:
                    logger.warning("Langfuse flush failed for task %s in finally block", task_id, exc_info=True)
            if session_manager is not None:
                try:
                    await session_manager.close()
                except Exception:
                    logger.warning("MCP session close failed for task %s in finally block", task_id, exc_info=True)
            if sandbox is not None:
                try:
                    await provisioner.destroy(sandbox)
                except Exception:
                    logger.warning("Sandbox destroy failed for task %s in finally block", task_id, exc_info=True)
```

### Step 7: Add time import

Ensure `time` is imported at the top of `graph.py`:

```python
import time
```

### Step 8: Write unit tests

Add to `services/worker-service/tests/test_executor.py`:

```python
class TestSandboxLifecycle:
    @pytest.mark.asyncio
    async def test_execute_task_no_sandbox_config_skips(self):
        """Task without sandbox config behaves identically to before."""
        executor = build_test_executor()
        task_data = build_test_task_data()
        # No sandbox key in agent_config
        cancel_event = asyncio.Event()

        # Should execute normally without sandbox
        # (full execution mocking omitted — validate sandbox is None path)
        sandbox_config = json.loads(task_data["agent_config_snapshot"]).get("sandbox", {})
        assert not sandbox_config.get("enabled", False)

    @pytest.mark.asyncio
    async def test_sandbox_provision_failure_dead_letters(self):
        """Sandbox provision failure → dead-letter with sandbox_provision_failed."""
        executor = build_test_executor()
        executor._sandbox_provisioner = MagicMock()
        executor._sandbox_provisioner.provision = AsyncMock(
            side_effect=SandboxProvisionError("python-3.11", "E2B API down")
        )
        executor._handle_dead_letter = AsyncMock()

        task_data = build_test_task_data(sandbox_enabled=True)
        cancel_event = asyncio.Event()

        await executor.execute_task(task_data, cancel_event)

        executor._handle_dead_letter.assert_called_once()
        call_kwargs = executor._handle_dead_letter.call_args
        assert "sandbox_provision_failed" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_sandbox_crash_recovery_success(self):
        """Task with sandbox_id reconnects to existing sandbox."""
        executor = build_test_executor()
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-existing"
        executor._sandbox_provisioner = MagicMock()
        executor._sandbox_provisioner.connect = AsyncMock(return_value=mock_sandbox)

        task_data = build_test_task_data(sandbox_enabled=True, sandbox_id="sbx-existing")
        # Verify connect is called with the sandbox_id
        # (full execution mocking omitted)

    @pytest.mark.asyncio
    async def test_sandbox_crash_recovery_failure_dead_letters(self):
        """Sandbox reconnect failure → dead-letter with sandbox_lost."""
        executor = build_test_executor()
        executor._sandbox_provisioner = MagicMock()
        executor._sandbox_provisioner.connect = AsyncMock(
            side_effect=SandboxConnectionError("sbx-expired", "not found")
        )
        executor._handle_dead_letter = AsyncMock()

        task_data = build_test_task_data(sandbox_enabled=True, sandbox_id="sbx-expired")
        cancel_event = asyncio.Event()

        await executor.execute_task(task_data, cancel_event)

        executor._handle_dead_letter.assert_called_once()
        call_kwargs = executor._handle_dead_letter.call_args
        assert "sandbox_lost" in str(call_kwargs)

    def test_sandbox_cost_calculation(self):
        """Verify sandbox cost formula: duration_seconds * vcpu * $0.05/3600."""
        duration_seconds = 600  # 10 minutes
        vcpu = 2
        # Expected: 600 * 2 * 50000 / 3600 = 16666 microdollars
        expected = int(duration_seconds * vcpu * 50000 / 3600)
        assert expected == 16666

    def test_sandbox_cost_calculation_small(self):
        """Verify sandbox cost for minimal usage."""
        duration_seconds = 60  # 1 minute
        vcpu = 1
        expected = int(duration_seconds * vcpu * 50000 / 3600)
        assert expected == 833
```

## Acceptance Criteria

- [ ] `GraphExecutor` has lazy-initialized `sandbox_provisioner` property
- [ ] `execute_task()` provisions sandbox when `agent_config.sandbox.enabled` is true
- [ ] `sandbox_id` stored in DB immediately after provisioning
- [ ] Crash recovery: reconnects via `provisioner.connect(sandbox_id)` when `task_data["sandbox_id"]` exists
- [ ] Failed reconnect → dead-letter with reason `sandbox_lost`
- [ ] Failed provision → dead-letter with reason `sandbox_provision_failed`
- [ ] Missing `E2B_API_KEY` → dead-letter with reason `sandbox_provision_failed`
- [ ] HITL pause: `provisioner.pause(sandbox)` called before releasing lease
- [ ] Budget pause: `provisioner.pause(sandbox)` called before releasing lease
- [ ] Sandbox destroyed on task completion
- [ ] `sandbox_id` cleared in DB on task completion
- [ ] Sandbox cost calculated: `duration_seconds * vcpu * $0.05/3600` in microdollars
- [ ] Sandbox cost recorded in `agent_cost_ledger` with checkpoint_id='sandbox'
- [ ] Sandbox cost logged at INFO level
- [ ] `finally` block destroys sandbox if still alive
- [ ] `_get_tools()` called with `sandbox` and `s3_client` when sandbox provisioned
- [ ] `_build_graph()` receives `injected_files` for system message generation
- [ ] Input file injection called after sandbox provisioning (from Task 6)
- [ ] Tasks without sandbox config behave identically to before (no regression)
- [ ] All unit tests pass
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests:** No sandbox config skips provisioning. Provision failure dead-letters. Crash recovery success. Crash recovery failure dead-letters. Cost calculation formula verification. Missing E2B_API_KEY dead-letters.
- **Regression:** `make test` — all existing executor tests pass unchanged.

## Constraints and Guardrails

- Do not modify the SandboxProvisioner class — use it as-is from Task 2.
- Do not modify sandbox tool factory functions — use them as-is from Tasks 3-5.
- Preserve the existing MCP session lifecycle code — sandbox lifecycle runs alongside it.
- Sandbox errors (pause, destroy) in the finally block must be caught and logged, never raised.
- The `_handle_dead_letter()` method must be used for dead-lettering — do NOT create a new helper.
- The `sandbox` variable must be set to `None` after explicit close/destroy to prevent double-destroy in the finally block.
- Cost tracking failures must be caught and logged — never prevent task completion.

## Assumptions

- Task 2 has been completed (`SandboxProvisioner` with `provision()`, `connect()`, `pause()`, `resume()`, `destroy()` exists).
- Tasks 3-5 have been completed (sandbox tool factory functions exist in `sandbox_tools.py`).
- Task 6 has been completed (`_inject_input_files()` and `_build_input_files_system_message()` methods exist on `GraphExecutor`).
- Task 1 has been completed (`sandbox_id` column exists on `tasks` table, `dead_letter_reason` CHECK includes `sandbox_lost` and `sandbox_provision_failed`).
- Track 1 Task 3 has been completed (`S3Client` exists for input file download).
- `agent_cost_ledger` table exists (from Phase 2 Track 3 migration 0007).
- The `time` module is available for `time.monotonic()` timing.
- `task_data` dict includes `sandbox_id` field when read from the DB (nullable).

<!-- AGENT_TASK_END: task-7-crash-recovery-and-cost.md -->
