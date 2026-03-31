<!-- AGENT_TASK_START: task-3-worker-refactor.md -->

# Task 3: Worker Per-Task Langfuse + Cost Restore

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files:
1. `docs/design/langfuse-customer-integration/design.md`
2. `services/worker-service/executor/graph.py` (current Langfuse integration — this is the primary file you will modify)
3. `services/worker-service/core/config.py` (WorkerConfig with Langfuse fields to remove)
4. `services/worker-service/main.py` (startup assertion to remove)
5. `services/worker-service/core/poller.py` (task claim query — needs new column)
6. `infrastructure/database/migrations/0003_dynamic_models.sql` (models table with cost rates)

## Context
The worker currently reads Langfuse credentials from environment variables (global config) and fails at startup if Langfuse is unreachable. This task refactors the worker to resolve Langfuse credentials per-task from the `langfuse_endpoints` database table, and restores internal cost/token tracking that was removed when the platform-owned Langfuse integration was added.

## Task-Specific Shared Contract
- Langfuse credentials are resolved per-task from `langfuse_endpoints` table using the task's `langfuse_endpoint_id`.
- If a task has no `langfuse_endpoint_id`, Langfuse is skipped entirely — zero overhead.
- All Langfuse operations (client init, callback attachment, flush) must be wrapped in try/except. Failures log a warning but never fail a task.
- Internal cost tracking is restored: `cost_microdollars` and `execution_metadata` are written to checkpoint rows using the existing (currently unused) columns.
- The `models` table has `input_microdollars_per_million` and `output_microdollars_per_million` columns for cost calculation.

## Affected Component
- **Service/Module:** Worker Service
- **File paths:** `services/worker-service/executor/graph.py`, `services/worker-service/core/config.py`, `services/worker-service/main.py`, `services/worker-service/core/poller.py`
- **Change type:** modification

## Dependencies
- **Must complete first:** Task 1 (database schema — `langfuse_endpoints` table and `tasks.langfuse_endpoint_id` column)
- **Provides output to:** Tasks 4, 5
- **Shared interfaces/contracts:** Per-task Langfuse credential resolution pattern, checkpoint cost data format.

## Implementation Specification

### Step 1: Remove Global Langfuse Config

Modify `core/config.py`:
- Remove fields: `langfuse_enabled`, `langfuse_host`, `langfuse_public_key`, `langfuse_secret_key`
- Remove the `__post_init__` validation block that checks these fields

Modify `main.py`:
- Remove the `_assert_langfuse_ready()` function entirely
- Remove its call and the associated `SystemExit` handler
- Keep the `from urllib.request import urlopen` import removal if it becomes unused

### Step 2: Per-Task Langfuse Resolution in GraphExecutor

Modify `executor/graph.py`:

Remove from `__init__`:
- `self._langfuse_client = self._initialize_langfuse_client()`
- The `_initialize_langfuse_client()` method entirely

Add new async method:
```python
async def _resolve_langfuse_credentials(self, endpoint_id: str) -> dict | None:
    """Query langfuse_endpoints table for credentials. Returns {host, public_key, secret_key} or None."""
```
This queries the `langfuse_endpoints` table by `endpoint_id` using `self.pool`.

Modify `execute_task()`:
- Read `langfuse_endpoint_id` from `task_data` (it will be in the claimed task row)
- If present, call `_resolve_langfuse_credentials()` to get `{host, public_key, secret_key}`
- If credentials resolve, create a per-execution `Langfuse` client and `CallbackHandler` inside a try/except
- On auth failure or any exception: log warning at `logging.WARNING` level, set credentials to `None`, continue without traces
- Pass credentials (or None) to `_build_runnable_config()`
- In the `finally` block: if a per-task Langfuse client was created, call `client.flush()` wrapped in try/except

Modify `_build_langfuse_callback()`:
- Change signature to accept explicit `host: str, public_key: str, secret_key: str` parameters
- Remove all references to `self.config.langfuse_*`
- The `CallbackHandler` constructor should use the explicit `public_key` and `secret_key` parameters

Modify `_build_runnable_config()`:
- Change signature to accept optional `langfuse_credentials: dict | None` parameter
- If `langfuse_credentials` is not None, build callback using the explicit credentials
- If None, skip callback attachment entirely

### Step 3: Update Task Claim Query

Modify the task claim SQL (in `core/poller.py` or the query builder function):
- Add `langfuse_endpoint_id` to the SELECT column list so it's available in `task_data`

### Step 4: Restore Internal Cost Tracking

Modify `executor/graph.py` to add cost extraction after LLM responses:

Add a helper method:
```python
async def _calculate_step_cost(self, response_metadata: dict, model_name: str) -> tuple[int, dict]:
    """Extract tokens from response metadata and calculate cost in microdollars.
    Returns (cost_microdollars, execution_metadata_dict)."""
```

This method:

**1. Token extraction** — LangChain response metadata varies by provider. Try these paths in order:
```python
def _extract_tokens(metadata: dict) -> tuple[int, int]:
    """Returns (input_tokens, output_tokens). Falls back to (0, 0) if not found."""
    usage = (
        metadata.get("usage")              # Anthropic, Google
        or metadata.get("token_usage")     # OpenAI via LangChain
        or metadata.get("usage_metadata")  # Bedrock
        or {}
    )
    input_t = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    output_t = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return (int(input_t), int(output_t))
```
If metadata is missing or malformed, silently return (0, 0) — never fail the task over cost tracking.

**2. Cost rate lookup** — query `models` table for the model's `input_microdollars_per_million` and `output_microdollars_per_million`. Cache the result per model within a single task execution (avoid repeated DB hits). If the model is not found in the table, log a warning and use rate 0 (free).

**3. Cost calculation:**
```python
cost_microdollars = (input_tokens * input_rate + output_tokens * output_rate) // 1_000_000
```
Where `input_rate` = `input_microdollars_per_million` (microdollars per million tokens). Example: rate `3000000` means $3.00/M tokens. For 1000 input tokens: `1000 * 3000000 // 1_000_000 = 3000` microdollars = $0.003. Integer division (floor) — no floating point.

**4. Returns** `(cost_microdollars, {"input_tokens": N, "output_tokens": N, "model": model_name})`

In the checkpoint save path:
- Accumulate cost per checkpoint
- Write to the existing `cost_microdollars` column (INT) and `execution_metadata` column (JSONB) on the checkpoint row
- These columns already exist in the schema but are currently unused

### Step 5: Graceful Degradation

Ensure all Langfuse operations follow this pattern:
```python
try:
    # Langfuse operation
except Exception:
    logger.warning("Langfuse operation failed for task %s, continuing without traces", task_id, exc_info=True)
```

Key principle: Langfuse failures must NEVER fail a task. The task must complete normally regardless of Langfuse availability.

## Acceptance Criteria
- [ ] Worker starts successfully without any Langfuse environment variables set.
- [ ] `WorkerConfig` no longer has Langfuse fields.
- [ ] `main.py` no longer has `_assert_langfuse_ready()`.
- [ ] Task with `langfuse_endpoint_id` pointing to a running Langfuse instance: traces appear in Langfuse.
- [ ] Task without `langfuse_endpoint_id`: completes normally, no Langfuse operations occur.
- [ ] Task with `langfuse_endpoint_id` pointing to unreachable host: completes normally with warning logged.
- [ ] Checkpoint rows have populated `cost_microdollars` and `execution_metadata` after task completion.
- [ ] Cost calculation matches model rates from the `models` table.

## Testing Requirements
- **Unit tests:** Test `_resolve_langfuse_credentials()` with valid/invalid endpoint IDs. Test `_calculate_step_cost()` with various response metadata formats.
- **Integration tests:**
  - Run task without Langfuse endpoint — verify completion and checkpoint cost data.
  - Run task with Langfuse endpoint pointing to test fixture — verify traces appear.
  - Run task with Langfuse endpoint pointing to unreachable host — verify completion with warning.
- **Failure scenarios:** Langfuse auth failure, Langfuse flush timeout, missing model in pricing table, malformed response metadata.

## Constraints and Guardrails
- Do not add any new environment variables for Langfuse.
- Do not add Langfuse as a startup dependency — the worker must start without any Langfuse instance.
- Cost tracking must work independently of Langfuse — even if Langfuse is not configured, checkpoints must have cost data.
- Keep the `langfuse` and `langfuse.langchain` imports — they are still needed for per-task usage.
