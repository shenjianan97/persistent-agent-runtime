<!-- AGENT_TASK_START: task-4-worker-interrupt.md -->

# Task 4 — Worker Interrupt Handling

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design/phase-2/PHASE2_MULTI_AGENT.md` — Section 7 (Human-in-the-Loop Input), Section 8 (Reliability Additions)
2. `services/worker-service/executor/graph.py` — current execution flow, especially the exception chain at lines 399-416
3. `services/worker-service/core/reaper.py` — existing reaper scan queries
4. `services/worker-service/tools/definitions.py` — existing tool registration pattern
5. `services/worker-service/core/poller.py` — claim query (to verify it excludes waiting states)
6. LangGraph `interrupt()` documentation — understand `GraphInterrupt` exception and `Command(resume=...)` semantics

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/implementation_plan/phase-2/track-2/progress.md` to "Done".

## Context

Track 2 introduces human-in-the-loop workflows in the worker. When the LangGraph graph calls `interrupt()` (either via approval gates or the `request_human_input` tool), the graph raises a `GraphInterrupt` exception. The worker must catch this, transition the task to a waiting state, release the lease, and return so the worker can pick up other work. Later, when a human approves/responds via the API (Task 3), the API writes a documented HITL resume payload, transitions the task back to `queued`, and emits the normal `new_task` notification. Any worker can then claim the task and resume from the checkpoint with `Command(resume=...)`.

This task also adds the `request_human_input` built-in tool (the MVP HITL entry point) and extends the reaper to enforce human-input timeouts.

## Task-Specific Shared Contract

- `GraphInterrupt` is caught in the `execute_task()` exception chain, before the generic `except Exception`.
- Pause states release the lease and do not require heartbeat while waiting.
- Resume goes through the normal `queued` claim path; any worker can pick up the resumed task.
- The `human_response` column stores a documented HITL resume payload for consumption on resume.
- Worker clears `human_response` after reading it to avoid PII retention.
- Default human-input-timeout is 24 hours (hardcoded for now, configurable per-agent in future tracks).
- Worker-side event writes use direct asyncpg INSERT, but they must be part of the same transaction as the paired task-state mutation.

## Affected Component

- **Service/Module:** Worker Service — Interrupt Handling
- **File paths:**
  - `services/worker-service/executor/graph.py` (modify — catch GraphInterrupt, implement _handle_interrupt, implement resume path)
  - `services/worker-service/core/reaper.py` (modify — add human-input-timeout scan)
  - `services/worker-service/tools/definitions.py` (modify — add request_human_input tool)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 1 (Database Migration — new statuses and columns), Task 2 (Event Service — durable event-recording contract), Task 3 (HITL API — approve/reject/respond transitions waiting tasks back to `queued` with `human_response`)
- **Provides output to:** Task 5 (Event Integration — worker-side event emission pattern), Task 7 (Integration Tests)
- **Shared interfaces/contracts:** Worker reads `human_response` from task row on resume; API writes it on approve/reject/respond

## Implementation Specification

### Step 1: Add request_human_input built-in tool

In `tools/definitions.py`, add a new tool that calls LangGraph's `interrupt()`:

```python
from langgraph.types import interrupt

def request_human_input(prompt: str) -> str:
    """Request input from a human operator. The task will pause until a response is provided."""
    response = interrupt({"type": "input", "prompt": prompt})
    return response
```

Register this as a `StructuredTool` following the pattern of the existing tools (web_search, calculator, etc.). Add it to the default tool set so agents can use it via `allowed_tools: ["request_human_input"]`.

Define the tool schema:
```python
REQUEST_HUMAN_INPUT_TOOL = {
    "name": "request_human_input",
    "description": "Request input from a human operator. The task will pause and wait for a human to respond.",
    "parameters": {
        "prompt": {"type": "string", "description": "The question or request to present to the human operator"}
    }
}
```

### Step 2: Import GraphInterrupt and add exception handler

In `executor/graph.py`:

```python
from langgraph.errors import GraphRecursionError, GraphInterrupt
```

Add to the exception chain in `execute_task()`, after `GraphRecursionError` and before `LeaseRevokedException`:

```python
except GraphInterrupt as gi:
    await self._handle_interrupt(task_data, gi, worker_id)
```

### Step 3: Implement _handle_interrupt

```python
async def _handle_interrupt(self, task_data: dict, interrupt: GraphInterrupt, worker_id: str):
    """Handle LangGraph interrupt by transitioning task to a waiting state."""
    task_id = str(task_data["task_id"])
    tenant_id = task_data["tenant_id"]
    agent_id = task_data["agent_id"]

    # Inspect interrupt value to determine type
    interrupt_values = interrupt.args[0] if interrupt.args else [{}]
    # interrupt_values is a list of interrupt payloads
    interrupt_data = interrupt_values[0] if isinstance(interrupt_values, list) and interrupt_values else {}
    if isinstance(interrupt_data, dict):
        interrupt_type = interrupt_data.get("type", "input")
    else:
        interrupt_type = "input"

    if interrupt_type == "approval":
        new_status = "waiting_for_approval"
        pending_field = "pending_approval_action"
        pending_value = json.dumps(interrupt_data.get("action", {}))
        event_type = "task_approval_requested"
    else:
        new_status = "waiting_for_input"
        pending_field = "pending_input_prompt"
        pending_value = interrupt_data.get("prompt", "Agent is requesting input")
        event_type = "task_input_requested"

    # Atomically transition task to waiting state and release the lease.
    # Use a single acquired connection/transaction for the task-row UPDATE and the
    # corresponding task_events INSERT so the pause transition and audit history
    # either both commit or both roll back.
```

Implementation notes:
- Clear `lease_owner` / `lease_expiry` when transitioning to `waiting_for_approval` / `waiting_for_input`.
- Set the relevant pending HITL field plus `human_input_timeout_at`.
- Record `task_approval_requested` / `task_input_requested` in the same transaction.
- Return control to the caller so the execution slot and lease are both freed.

### Step 4: Implement worker-side event recording helper

```python
async def _insert_task_event(conn, task_id: str, tenant_id: str, agent_id: str,
                             event_type: str, status_before: str, status_after: str,
                             worker_id: str | None, error_code: str = None,
                             error_message: str = None, details: dict | None = None):
    """Insert a task event on the current transaction-scoped connection."""
    await conn.execute(
        '''INSERT INTO task_events (tenant_id, task_id, agent_id, event_type,
                                    status_before, status_after, worker_id,
                                    error_code, error_message, details)
           VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)''',
        tenant_id, task_id, agent_id, event_type,
        status_before, status_after, worker_id,
        error_code, error_message, json.dumps(details or {})
    )
```

Callers should use this helper inside the same transaction as the paired `tasks` row mutation. Do not swallow INSERT failures.

### Step 5: Implement stateless resume path

Task 4 should reuse the existing claim/heartbeat execution flow:
- Do not add any new worker-specific wake channel.
- Do not extend `core/heartbeat.py` or `core/poller.py` for paused tasks.
- When a human responds, Task 3 re-queues the task through the existing `new_task` path; a normal claim starts execution again and then resumes from the checkpoint.

In the resume path inside `execute_task()`, after the existing checkpoint resume check, read and inject the human response:

```python
is_first_run = not checkpoint_tuple
initial_input = {"messages": [HumanMessage(content=task_input)]} if is_first_run else None

# Check for pending HITL resume payload (resume from a re-queued paused task)
if not is_first_run:
    human_response = await self.pool.fetchval(
        '''SELECT human_response FROM tasks WHERE task_id = $1::uuid''',
        task_id
    )
    if human_response:
        # Decode the documented HITL payload envelope before resuming.
        # Example payloads:
        #   {"kind":"input","message":"blue"}
        #   {"kind":"approval","approved":true}
        #   {"kind":"approval","approved":false,"reason":"Not safe"}
        # Clear human_response to avoid PII retention
        await self.pool.execute(
            '''UPDATE tasks SET human_response = NULL WHERE task_id = $1::uuid''',
            task_id
        )
        # Use Command(resume=...) to provide the interrupt response to LangGraph
        from langgraph.types import Command
        initial_input = Command(resume=decoded_payload)
```

The `Command(resume=value)` tells LangGraph to resolve the pending `interrupt()` call with the given value. The graph then continues execution from the interrupt point after a normal `queued` → `running` claim.

### Step 6: Add reaper scan for human-input-timeout

In `reaper.py`, add a new scan method (called from `run_once()`):

```python
async def _timeout_waiting_tasks(self):
    """Dead-letter tasks that have exceeded their human input timeout."""
    rows = await self.pool.fetch(
        '''UPDATE tasks
           SET status = 'dead_letter',
               dead_letter_reason = 'human_input_timeout',
               last_error_code = 'human_input_timeout',
               last_error_message = 'No human response within timeout period',
               dead_lettered_at = NOW(),
               pending_input_prompt = NULL,
               pending_approval_action = NULL,
               human_input_timeout_at = NULL,
               version = version + 1,
               updated_at = NOW()
           WHERE status IN ('waiting_for_approval', 'waiting_for_input')
             AND human_input_timeout_at IS NOT NULL
             AND human_input_timeout_at < NOW()
           RETURNING task_id, tenant_id, agent_id'''
    )
    for row in rows:
        logger.info("Task %s dead-lettered: human_input_timeout", row["task_id"])
```

Add a call to `_timeout_waiting_tasks()` in the reaper's `run_once()` method, alongside the existing lease-expiry and task-timeout scans.

## Acceptance Criteria

- [ ] `request_human_input` tool is registered and available to agents
- [ ] Graph calling `interrupt()` via `request_human_input` → task transitions to `waiting_for_input`
- [ ] Worker releases lease ownership when task enters a waiting state
- [ ] `_handle_interrupt` records its task event in the same transaction as the waiting-state transition
- [ ] On resume from checkpoint, worker reads `human_response` from task row
- [ ] Worker decodes the documented HITL resume payload before calling `Command(resume=...)`
- [ ] Worker clears `human_response` after reading (PII hygiene)
- [ ] Resume uses `Command(resume=decoded_payload)` to resolve the LangGraph interrupt
- [ ] Resume happens after a second normal claim from the task poller
- [ ] Reaper dead-letters tasks past `human_input_timeout_at` with reason `human_input_timeout`

## Testing Requirements

- **Unit tests:** Mock graph that calls `interrupt()` → verify `_handle_interrupt` called, task status updated, and lease ownership released. Mock resume with `human_response` → verify `Command(resume=...)` used.
- **Integration tests:** Submit task with `request_human_input` tool → verify task enters `waiting_for_input` with no active lease. Respond via API → verify a worker claims the re-queued task and completes it.
- **Failure scenarios:** Interrupt when lease already lost → graceful warning log. Reaper timeout → task dead-lettered with correct reason.

## Constraints and Guardrails

- Do not implement approval gates for non-idempotent tools — Track 5 handles that.
- Do not implement budget-based pause logic — Track 3 handles that.
- The 24-hour timeout is hardcoded. Do not add per-agent configuration yet.
- Task/event mutations for pause and resume must be atomic; do not swallow event INSERT failures.

## Assumptions

- LangGraph 1.0.5 supports `interrupt()`, `GraphInterrupt`, and `Command(resume=...)`. These are stable APIs in the current version.
- The `interrupt()` call returns a list of interrupt values. The first value contains the interrupt metadata.
- The checkpoint persisted before the interrupt contains the full graph state, so resume starts from the correct point.
- The worker's semaphore and lease are both released when the graph pauses; resumed work restarts through the existing claim path.

<!-- AGENT_TASK_END: task-4-worker-interrupt.md -->
