<!-- AGENT_TASK_START: task-7-integration-tests.md -->

# Task 7 — Integration Tests + Worker Compatibility

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design/phase-2/track-1-agent-control-plane.md` — canonical design contract
2. `tests/backend-integration/helpers/api_client.py` — existing test API client
3. `tests/backend-integration/helpers/e2e_context.py` — existing E2E context helper
4. `tests/backend-integration/conftest.py` — test fixtures and cleanup
5. `tests/backend-integration/helpers/db.py` — database helper for cleanup
6. `services/worker-service/core/poller.py` — worker claim query
7. All `tests/backend-integration/test_*.py` files — existing test suites

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/implementation_plan/phase-2/track-1/progress.md` to "Done".

## Context

The backend integration tests must be updated for the new agent-based submission contract. The `ApiClient.submit_task()` helper currently builds an inline `agent_config` payload that the API no longer accepts. Every test that submits a task must first ensure an agent exists. Worker test fixtures that directly INSERT task rows must also insert agent rows first to satisfy the FK constraint.

## Task-Specific Shared Contract

- `submit_task()` no longer sends `agent_config`. It sends only `agent_id` plus task-level fields.
- Every test that submits a task must first create or ensure an agent exists.
- Test cleanup must respect FK ordering: delete tasks before deleting agents.
- The worker's claim query uses `RETURNING t.*`, so the new `agent_display_name_snapshot` column is automatically included — no worker code changes needed.
- Worker test fixtures that directly INSERT tasks need an agent row first for FK compliance.

## Affected Component

- **Service/Module:** Integration Tests, Worker Service (test fixtures only)
- **File paths:**
  - `tests/backend-integration/helpers/api_client.py` (modify)
  - `tests/backend-integration/helpers/e2e_context.py` (modify)
  - `tests/backend-integration/conftest.py` (modify — cleanup order)
  - `tests/backend-integration/helpers/db.py` (modify — add `ensure_agent()`, update `insert_task()` for FK compliance, update `clean()` for FK ordering)
  - `tests/backend-integration/test_happy_path.py` (modify)
  - `tests/backend-integration/test_validation.py` (modify)
  - `tests/backend-integration/test_redrive.py` (verify — uses `db.insert_task()` directly)
  - `tests/backend-integration/test_reaper_edges.py` (verify — uses `db.insert_task()` directly)
  - `tests/backend-integration/test_tenant_isolation.py` (verify — uses `db.insert_task()` directly)
  - All other `tests/backend-integration/test_*.py` files that call `submit_task()` (modify)
  - `tests/backend-integration/test_agents.py` (new — dedicated agent CRUD tests)
  - `services/worker-service/tests/test_integration.py` (modify — INSERT agent rows before task rows at `setup_test_task()` line 33 and inline INSERTs at lines 165, 184)
  - `services/worker-service/tests/test_checkpointer_integration.py` (modify — `_insert_task()` helper at line 38 is used by 7+ test cases and must insert agent row first for FK compliance)
  - `services/worker-service/tests/test_executor.py` (verify — may need task_data fixture update)
- **Change type:** modification + new test file

## Dependencies

- **Must complete first:** Tasks 1-4 (all backend changes: schema, CRUD API, submission refactor, response enrichment)
- **Provides output to:** None (final validation task)

## Implementation Specification

### Step 1: Add agent CRUD methods to ApiClient

Add to `tests/backend-integration/helpers/api_client.py`.

**IMPORTANT — URL path convention:** The existing `API_BASE` in `conftest.py` is `http://localhost:8080/v1`, and all existing helpers use paths relative to that base (e.g., `/tasks`, not `/v1/tasks`). New agent helper methods must follow the same convention — use `/agents`, not `/v1/agents`.

```python
def create_agent(self, agent_id="e2e_agent", display_name="E2E Test Agent",
                 agent_config=None, **overrides):
    """Create an agent. Returns response dict."""
    config = agent_config or {
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["calculator"]
    }
    payload = {
        "agent_id": agent_id,
        "display_name": display_name,
        "agent_config": config,
        **overrides
    }
    return self._request("POST", "/agents", payload, expected_status=201)

def get_agent(self, agent_id):
    """Get agent detail. Returns response dict."""
    return self._request("GET", f"/agents/{agent_id}")

def list_agents(self, status=None, limit=None):
    """List agents. Returns list."""
    params = {}
    if status: params["status"] = status
    if limit: params["limit"] = str(limit)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    path = f"/agents{'?' + query if query else ''}"
    return self._request("GET", path)

def update_agent(self, agent_id, display_name, agent_config, status):
    """Update agent. Returns response dict."""
    payload = {
        "display_name": display_name,
        "agent_config": agent_config,
        "status": status
    }
    return self._request("PUT", f"/agents/{agent_id}", payload)
```

### Step 2: Modify submit_task()

Update `ApiClient.submit_task()` to use the new contract.

**IMPORTANT — Do not silently strip legacy kwargs.** Tests that passed inline config overrides (e.g., `model=`, `allowed_tools=`) were intentionally exercising specific runtime paths. Silently discarding those keys would mask broken test migrations and hide regressions. Instead, reject them loudly so every caller is explicitly updated.

```python
# Legacy keys that are no longer part of the task submission contract.
# Tests that need specific agent configs must create a dedicated agent instead.
_LEGACY_AGENT_CONFIG_KEYS = {"agent_config", "system_prompt", "provider", "model", "temperature", "allowed_tools"}

def submit_task(self, *, expected_status: int | tuple[int, ...] = 201,
                raise_for_status: bool = True, **overrides):
    """Submit a task referencing an existing agent."""
    # Fail fast if callers pass legacy inline-config keys
    legacy = _LEGACY_AGENT_CONFIG_KEYS & overrides.keys()
    if legacy:
        raise TypeError(
            f"submit_task() received legacy agent_config keys {legacy}. "
            "Inline agent config is no longer supported. "
            "Create a dedicated agent with the required config and pass its agent_id instead."
        )
    payload = {
        "agent_id": overrides.pop("agent_id", "e2e_agent"),
        "input": overrides.pop("input", "What is 2+2?"),
        "max_retries": overrides.pop("max_retries", 3),
        "max_steps": overrides.pop("max_steps", 10),
        "task_timeout_seconds": overrides.pop("task_timeout_seconds", 120),
    }
    if "langfuse_endpoint_id" in overrides:
        payload["langfuse_endpoint_id"] = overrides.pop("langfuse_endpoint_id")
    if "tenant_id" in overrides:
        payload["tenant_id"] = overrides.pop("tenant_id")
    return self._request("POST", "/tasks", payload, expected_status, raise_for_status)
```

Note the path is `/tasks` (not `/v1/tasks`) to match the existing `API_BASE` convention.

### Step 3: Add ensure_agent() to E2EContext

```python
def ensure_agent(self, agent_id="e2e_agent", **kwargs):
    """Create agent if it doesn't already exist (catches 409)."""
    try:
        return self.api.create_agent(agent_id=agent_id, **kwargs)
    except ApiError as e:
        if e.status_code == 409:
            return self.api.get_agent(agent_id)
        raise
```

### Step 4: Update conftest.py cleanup

The `db_pool` fixture in `conftest.py` has TWO cleanup blocks — one pre-yield (lines 254-257) and one post-yield (lines 259-262). **Both** must be updated to include `DELETE FROM agents` in FK-safe order:

```python
# Clean up in FK-safe order: tasks first, then agents
# This block appears TWICE in db_pool — update both pre-yield and post-yield
await conn.execute("DELETE FROM checkpoint_writes")
await conn.execute("DELETE FROM checkpoints")
await conn.execute("DELETE FROM tasks")
await conn.execute("DELETE FROM agents")
```

### Step 5: Update existing API-based tests

The updated `submit_task()` will raise `TypeError` if any legacy inline-config kwargs are passed. This is intentional — it forces every test to be explicitly migrated rather than silently losing coverage.

For each test file that calls `submit_task()`:
- Add `ctx.ensure_agent()` (or equivalent) before submitting tasks
- If a test previously passed overrides like `model=`, `allowed_tools=`, `temperature=`, etc., create a dedicated agent with the required config via `ctx.api.create_agent(agent_id="test-specific-agent", agent_config={...})` and pass `agent_id="test-specific-agent"` to `submit_task()`
- Tests that only passed default overrides (or none) can use the shared `e2e_agent` from `ensure_agent()`
- Validation tests (e.g., "submit with invalid model") should move to agent CRUD tests (e.g., "create agent with invalid model returns 400") since config validation now happens at agent creation time, not task submission time

### Step 5b: Update DbHelper.insert_task() and all direct task fixture callers

**IMPORTANT:** Several integration tests bypass the API and insert task rows directly via `DbHelper.insert_task()` in `tests/backend-integration/helpers/db.py`. Once the FK constraint `fk_tasks_agent` is in place, these direct INSERTs will fail because no matching agent row exists. This is a critical migration step — the full integration test suite will be broken if this is missed.

**Files affected:**
- `tests/backend-integration/helpers/db.py` — `insert_task()` method (line 106)
- `tests/backend-integration/test_redrive.py` — calls `e2e.db.insert_task()`
- `tests/backend-integration/test_reaper_edges.py` — calls `e2e.db.insert_task()`
- `tests/backend-integration/test_tenant_isolation.py` — calls `e2e.db.insert_task()`

**Required changes to `DbHelper`:**

Add an `ensure_agent()` method to `DbHelper` that inserts an agent row if it doesn't already exist:

```python
async def ensure_agent(
    self,
    *,
    tenant_id: str = "default",
    agent_id: str = "e2e_agent",
    display_name: str = "E2E Test Agent",
) -> None:
    """Insert agent row if it doesn't exist (for FK compliance)."""
    agent_config = json.dumps({
        "provider": "anthropic",
        "system_prompt": "You are a test assistant.",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["calculator"],
    })
    await self.execute(
        """
        INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
        VALUES ($1, $2, $3, $4::jsonb, 'active')
        ON CONFLICT (tenant_id, agent_id) DO NOTHING
        """,
        tenant_id, agent_id, display_name, agent_config,
    )
```

Update `insert_task()` to call `ensure_agent()` before inserting the task:

```python
async def insert_task(self, *, tenant_id="default", agent_id="e2e_agent", ...):
    # Ensure the referenced agent exists (FK compliance)
    await self.ensure_agent(tenant_id=tenant_id, agent_id=agent_id)
    # ... existing INSERT logic ...
```

Also update `DbHelper.clean()` to respect FK ordering:

```python
async def clean(self) -> None:
    async with self.pool.acquire() as conn:
        await conn.execute("DELETE FROM checkpoint_writes")
        await conn.execute("DELETE FROM checkpoints")
        await conn.execute("DELETE FROM tasks")
        await conn.execute("DELETE FROM agents")
```

**No changes needed to the test files themselves** — `insert_task()` will transparently ensure agents exist before inserting tasks.

### Step 6: Verify worker compatibility

- Check `services/worker-service/core/poller.py` claim query — it uses `RETURNING t.*` or `SELECT t.*`, so the new `agent_display_name_snapshot` column is automatically included. No code change needed.
- Check `services/worker-service/executor/graph.py` — it reads `task_data["agent_config_snapshot"]`. This field is still populated via snapshot at submission time. No code change needed.
- Update `services/worker-service/tests/test_integration.py`: `setup_test_task()` (line 33) and inline INSERTs (lines 165, 184) must first INSERT INTO agents for FK compliance.
- Update `services/worker-service/tests/test_checkpointer_integration.py`: the `_insert_task()` helper (line 38) is called by 7+ test cases and must insert an agent row before inserting the task. Add an `_ensure_agent()` helper similar to `DbHelper.ensure_agent()`.
- Check `services/worker-service/tests/test_executor.py`: if task_data dicts are constructed directly, add `agent_display_name_snapshot` key (can be null/None).

### Step 7: Create dedicated agent CRUD tests

Create `tests/backend-integration/test_agents.py`:

```python
class TestAgentCRUD:
    def test_create_agent(self):
        """POST /v1/agents creates agent, returns 201."""

    def test_create_agent_duplicate_returns_409(self):
        """POST /v1/agents with existing agent_id returns 409."""

    def test_list_agents(self):
        """GET /v1/agents returns agent list."""

    def test_list_agents_status_filter(self):
        """GET /v1/agents?status=active filters correctly."""

    def test_get_agent_detail(self):
        """GET /v1/agents/{id} returns full config."""

    def test_get_agent_not_found_returns_404(self):
        """GET /v1/agents/{unknown} returns 404."""

    def test_update_agent(self):
        """PUT /v1/agents/{id} updates and returns updated agent."""

    def test_update_agent_not_found_returns_404(self):
        """PUT /v1/agents/{unknown} returns 404."""

    def test_submit_task_with_disabled_agent_returns_400(self):
        """POST /v1/tasks with disabled agent returns 400."""

    def test_submit_task_with_unknown_agent_returns_404(self):
        """POST /v1/tasks with unknown agent_id returns 404."""

    def test_submit_task_snapshots_display_name(self):
        """POST /v1/tasks snapshots display_name, visible in GET /v1/tasks/{id}."""

    def test_agent_edit_does_not_affect_existing_task(self):
        """Editing agent after task submission doesn't change task snapshot."""
```

## Acceptance Criteria

- [ ] All existing integration tests pass with the new agent-based submission contract
- [ ] `test_agents.py` covers: create, list, get, update, duplicate (409), not found (404), disabled submission (400)
- [ ] Display name snapshot verified: create agent, submit task, check `agent_display_name` in task response
- [ ] Agent edit isolation verified: edit agent after submission, verify task snapshot unchanged
- [ ] `DbHelper.insert_task()` ensures agent row exists before inserting tasks (FK compliance)
- [ ] Direct-fixture tests (`test_redrive.py`, `test_reaper_edges.py`, `test_tenant_isolation.py`) pass with FK constraint
- [ ] `DbHelper.clean()` respects FK ordering (tasks deleted before agents)
- [ ] Worker tests (`pytest services/worker-service/tests/`) pass with FK constraint
- [ ] Database cleanup in `conftest.py` respects FK ordering (tasks deleted before agents)
- [ ] End-to-end flow works: create agent → submit task → worker executes → task completes → response includes `agent_display_name`

## Testing Requirements

- **Integration tests:** Full suite run: `make test-integration` (or equivalent) passes.
- **Worker tests:** `pytest services/worker-service/tests/` passes.
- **E2E flow:** If a local E2E harness exists, verify the full create-agent → submit → execute → complete cycle.

## Constraints and Guardrails

- Do not change worker execution logic. The worker reads `agent_config_snapshot` and does not need `agent_display_name_snapshot` for execution.
- Test cleanup must respect FK ordering: delete tasks before agents, delete checkpoint_writes before checkpoints.
- Legacy test patterns that pass inline config to `submit_task()` should be cleaned up, not silently ignored.
- Test agent IDs should be unique per test or cleaned up between tests to avoid interference.

## Assumptions

- Tasks 1-4 have been completed and the API accepts the new contract.
- The `test_seed.sql` from Task 1 provides a default `e2e_agent` for tests that don't create their own.
- The existing test infrastructure (pytest fixtures, database pool, API client) remains structurally the same.

<!-- AGENT_TASK_END: task-7-integration-tests.md -->
