<!-- AGENT_TASK_START: task-6-graph-executor.md -->

# Task 6: Graph Executor Assembly

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files to understand the system architecture and constraints:
1. `docs/PROJECT.md` 
2. `docs/design/PHASE1_DURABLE_EXECUTION.md`

**CRITICAL POST-WORK:** After completing this task, you MUST update the status of this task to "Done" in the `docs/implementation_plan/phase-1/progress.md` file.

## Context
The Graph Executor orchestrates the LangGraph instance mapping task API properties against active LLM components encapsulating failure parameters independently handling external tool integrations securely.

## Task-Specific Shared Contract
- Treat `docs/design/PHASE1_DURABLE_EXECUTION.md` as the canonical execution contract. Do not redefine retry policy, dead-letter reasons, or resume semantics here.
- This task consumes Task 3 worker-core primitives, Task 4 checkpointer behavior, and Task 5 MCP tool definitions. Reuse those interfaces instead of re-implementing them.
- `thread_id = task_id` is the resume key. Execution must use the custom checkpointer and `recursion_limit = max_steps`.
- Failure classification must follow the documented matrix:
- Retryable failures: transient provider 429/5xx, temporary network failures, recoverable MCP transport failures -> requeue with backoff.
- Non-retryable failures: provider 4xx invalid request, unsupported tool, invalid tool arguments -> dead-letter with `non_retryable_error`.
- `GraphRecursionError` -> dead-letter with `max_steps_exceeded`.
- `LeaseRevokedException` or detected lease loss -> stop execution immediately and let reaper/API state handle next transition.
- The executor owns LangGraph assembly, provider calls, tool dispatch, cost tracking, and final task completion logic.

## Affected Component
- **Service/Module:** Worker Service Graph Executor (Python)
- **File paths (if known):** `services/worker-service/executor/`
- **Change type:** new code

## Dependencies
- **Must complete first:** Task 3 (Worker Core), Task 4 (Checkpointer), Task 5 (MCP Server)
- **Provides output to:** None
- **Shared interfaces/contracts:** Utilizes custom objects mapping internal DB structures into generic LLM interface components dynamically.

## Implementation Specification
Step 1: Build the graph assembly function that takes a claimed task record and constructs a LangGraph `StateGraph` from the `agent_config_snapshot`. Configure the LLM provider (Bedrock/Anthropic) from the agent config's `model` field. Bind MCP tools from Task 5, filtered to the task's `allowed_tools`. Set `thread_id = task_id` for checkpoint resume. Set `recursion_limit = max_steps`.
Step 2: Wrap the `graph.astream()` call in `asyncio.timeout(task_timeout_seconds)` as defense-in-depth (the reaper provides external timeout enforcement, but the local timeout provides faster detection). Initialize the `PostgresDurableCheckpointer` from Task 4 with the current `worker_id` and `tenant_id`.
Step 3: Implement a `CostTrackingCallback` that accumulates token usage per LLM call and writes `cost_microdollars` into checkpoint metadata.
Step 4: Implement the failure classification handler following the documented matrix:
  - Retryable (transient provider 429/5xx, temporary network failures, recoverable MCP transport errors): update task with `last_error_code`/`last_error_message`, increment `retry_count`, set `retry_after` with exponential backoff, set `status='queued'`, and emit `pg_notify('new_task', worker_pool_id)`.
  - Non-retryable (provider 4xx, unsupported tool, invalid tool arguments): transition to `status='dead_letter'` with `dead_letter_reason='non_retryable_error'`.
  - `GraphRecursionError`: transition to `status='dead_letter'` with `dead_letter_reason='max_steps_exceeded'`.
  - `LeaseRevokedException` or detected lease loss: stop execution immediately, do not update task state (let reaper/API handle the next transition).
  - `asyncio.TimeoutError`: transition to `status='dead_letter'` with `dead_letter_reason='task_timeout'`.
Step 5: Implement the completion path: on successful graph termination, set `status='completed'`, write `output` from the final graph state, and increment `version`.
Step 6: Implement cancellation awareness: before each graph iteration, check if the task has been cancelled (heartbeat returning 0 rows signals this). On cancellation, stop execution gracefully.

## Acceptance Criteria
The implementation is complete when:
- [ ] Successful graph execution sets `status='completed'` with output from final state.
- [ ] Retryable LLM errors (429/5xx) requeue the task with backoff.
- [ ] Non-retryable errors (4xx) dead-letter the task with `non_retryable_error`.
- [ ] `GraphRecursionError` dead-letters with `max_steps_exceeded`.
- [ ] `LeaseRevokedException` stops execution without modifying task state.
- [ ] `asyncio.timeout` fires before `task_timeout_seconds` and dead-letters with `task_timeout`.
- [ ] Resumed execution (after crash recovery) continues from the last checkpoint, not from scratch.
- [ ] Cost tracking accumulates across checkpoints correctly.

## Testing Requirements
- **Unit tests:** Mock the LLM provider and MCP tools. Test each failure classification path (429, 5xx, 4xx, `GraphRecursionError`, `LeaseRevokedException`, `asyncio.TimeoutError`) produces the correct state transition. Test cost callback accumulation.
- **Integration tests:** Against a PostgreSQL test container with mock LLM: (a) run a task to completion and verify `status='completed'` with output, (b) simulate a crash mid-execution and verify resume from checkpoint, (c) verify cross-service NOTIFY: submit a task via the API, confirm the worker receives the NOTIFY and claims it within 2 seconds.

## Constraints and Guardrails
- Never inject untrusted parameters outside explicitly marked user prompt parameters dynamically conclusively transparently.
- Keep task boundaries clean: queue claiming, heartbeats, and reaper logic come from Task 3; this task should not quietly replace them.

## Assumptions / Open Questions for This Task
- None

<!-- AGENT_TASK_END: task-6-graph-executor.md -->
