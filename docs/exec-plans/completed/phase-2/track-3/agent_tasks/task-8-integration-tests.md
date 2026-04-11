<!-- AGENT_TASK_START: task-8-integration-tests.md -->

# Task 8 — Integration Tests: Scheduler, Budgets, Pause/Resume

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md` — canonical design contract (Testing Strategy section)
2. `tests/backend-integration/test_happy_path.py` — existing E2E test pattern
3. `tests/backend-integration/test_hitl_approval_flow.py` — HITL test patterns for pause/resume flows
4. `tests/backend-integration/test_agents.py` — agent CRUD test patterns
5. `tests/backend-integration/helpers/e2e_context.py` — E2EContext helper (ensure_agent, submit_task, wait_for_status, etc.)
6. `tests/backend-integration/helpers/api_client.py` — ApiClient methods

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-3/progress.md` to "Done".

## Context

Track 3 introduces significant runtime behavior changes: fair scheduling, concurrency caps, budget enforcement, and auto-recovery. These cross-cutting behaviors require end-to-end integration tests that exercise the full stack (API → database → worker → reaper → API).

## Task-Specific Shared Contract

- Tests use the existing `E2EContext` pattern from `helpers/e2e_context.py`.
- Tests should create agents with specific budget/concurrency settings and verify behavior.
- Budget tests may need a custom tool or agent config that generates predictable costs.
- Timing-sensitive tests (auto-recovery) should use the reaper's `run_once()` directly or wait for the next reaper cycle.
- All tests must be independent and not depend on execution order.
- Tests must clean up after themselves (existing E2E cleanup patterns).

## Affected Component

- **Service/Module:** Integration Tests
- **File paths:**
  - `tests/backend-integration/test_scheduler_fairness.py` (new)
  - `tests/backend-integration/test_budget_enforcement.py` (new)
  - `tests/backend-integration/test_budget_resume.py` (new)
  - `tests/backend-integration/helpers/api_client.py` (modify — add resume_task method)
  - `tests/backend-integration/helpers/e2e_context.py` (modify — add resume_task helper, update ensure_agent for budget fields)
- **Change type:** new code + modification

## Dependencies

- **Must complete first:** Task 1 (Schema), Task 2 (Incremental Cost), Task 3 (Scheduler Claim), Task 4 (Budget Enforcement), Task 5 (Reaper Recovery), Task 6 (API Extensions)
- **Provides output to:** None (final task)
- **Shared interfaces/contracts:** Full API contract, worker behavior, reaper behavior

## Implementation Specification

### Step 1: Extend E2E helpers

**IMPORTANT — Codebase conventions to follow:**
- `E2EContext` methods (`ensure_agent`, `submit_task`, `get_task`) are **synchronous** — do NOT use `await` on them. Only `wait_for_status`, `wait_for_statuses`, `start_worker`, `stop_worker`, and `wait_for` are async.
- The `ApiClient` uses `urllib`, NOT `requests`. Errors raise `ApiError` (from `helpers/api_client.py`), NOT `requests.HTTPError`.
- The conftest fixture is named `e2e`, NOT `ctx`. All test methods must use `e2e` as the parameter name.
- `update_agent()` currently requires all positional args `(agent_id, display_name, agent_config, status)` — it must be refactored to accept `**kwargs` for partial updates.
- `ensure_agent()` returns the full response dict `{"status_code": ..., "body": {...}}` — extract `agent_id` via `result["body"]["agent_id"]` or use `e2e._default_agent_id`.

**Add to `api_client.py`:**
```python
def resume_task(self, task_id: str) -> dict:
    """POST /tasks/{task_id}/resume"""
    return self._request("POST", f"/tasks/{task_id}/resume")

def list_tasks(self, **params) -> dict:
    """GET /tasks with optional filters (status, agent_id, pause_reason, limit)"""
    query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    path = f"/tasks?{query}" if query else "/tasks"
    return self._request("GET", path)

# Refactor update_agent to accept partial kwargs:
def update_agent(self, agent_id: str, **kwargs) -> dict:
    """PUT /agents/{agent_id} with partial update support.
    Fetches current agent state, merges kwargs, then PUTs the full payload."""
    current = self.get_agent(agent_id)["body"]
    payload = {
        "display_name": current["display_name"],
        "agent_config": current["agent_config"],
        "status": current["status"],
        "max_concurrent_tasks": current.get("max_concurrent_tasks", 5),
        "budget_max_per_task": current.get("budget_max_per_task", 500000),
        "budget_max_per_hour": current.get("budget_max_per_hour", 5000000),
    }
    payload.update(kwargs)
    return self._request("PUT", f"/agents/{agent_id}", payload)
```

**Add to `e2e_context.py`:**
```python
def resume_task(self, task_id: str) -> dict:
    return self.api.resume_task(task_id)

def get_events(self, task_id: str) -> list:
    resp = self.api.get_task_events(task_id)
    return resp["body"]["events"]
```

**Update `conftest.py` `_do_clean()`** to clean the new Track 3 tables:
```python
await conn.execute("DELETE FROM agent_cost_ledger")
await conn.execute("DELETE FROM agent_runtime_state")
```

### Step 2: Create test_scheduler_fairness.py

Test fair scheduling across multiple agents in one worker pool:

```python
class TestSchedulerFairness:

    async def test_round_robin_across_agents(self, e2e):
        """Two agents with queued tasks — claims should alternate between them."""
        agent_a = e2e.ensure_agent(agent_id="fairness-agent-a")
        agent_b = e2e.ensure_agent(agent_id="fairness-agent-b")

        # Submit 4 tasks for each agent (simple echo tasks)
        tasks_a = [e2e.submit_task(agent_id=agent_a["body"]["agent_id"]) for _ in range(4)]
        tasks_b = [e2e.submit_task(agent_id=agent_b["body"]["agent_id"]) for _ in range(4)]

        # Wait for all tasks to complete
        for t in tasks_a + tasks_b:
            await e2e.wait_for_status(t, "completed", timeout=60)

        # Verify: check task_events for claim order
        # Claims should alternate: A, B, A, B, ... (or B, A, B, A, ...)
        # The exact order depends on scheduler_cursor initialization,
        # but both agents should be served roughly equally
        events_a = e2e.get_events(tasks_a[0])
        events_b = e2e.get_events(tasks_b[0])
        # Verify fair scheduling: both agents should have at least 1 task
        # claimed within the first 4 claims (not all 4 going to one agent)
        all_events = []
        for t in tasks_a:
            events = e2e.get_events(t)
            claim_events = [e for e in events if e["event_type"] == "task_claimed"]
            all_events.extend([(e["created_at"], "a") for e in claim_events])
        for t in tasks_b:
            events = e2e.get_events(t)
            claim_events = [e for e in events if e["event_type"] == "task_claimed"]
            all_events.extend([(e["created_at"], "b") for e in claim_events])
        all_events.sort()  # Sort by claim time
        # First 4 claims should include both agents
        first_4_agents = {agent for _, agent in all_events[:4]}
        assert len(first_4_agents) == 2, f"Expected both agents in first 4 claims, got {first_4_agents}"

    async def test_concurrency_cap_enforcement(self, e2e):
        """Agent with max_concurrent_tasks=1 — only one task runs at a time."""
        agent = e2e.ensure_agent(
            agent_id="concurrency-agent",
            max_concurrent_tasks=1,
        )

        # Submit 3 tasks (use slow tasks to test concurrency)
        task_ids = [e2e.submit_task(agent_id=agent["body"]["agent_id"]) for _ in range(3)]

        # Wait briefly for first claim
        await e2e.wait_for_status(task_ids[0], "running", timeout=30)

        # Verify: only one task should be running
        statuses = [e2e.get_task(t) for t in task_ids]
        running_count = sum(1 for s in statuses if s["status"] == "running")
        assert running_count <= 1, f"Expected at most 1 running, got {running_count}"

    async def test_disabled_agent_not_scheduled(self, e2e):
        """Disabled agent's tasks are not claimed."""
        agent = e2e.ensure_agent(agent_id="disabled-agent", status="disabled")
        task_id = e2e.submit_task(agent_id=agent["body"]["agent_id"])

        # Wait briefly — task should remain queued
        await asyncio.sleep(5)
        task = e2e.get_task(task_id)
        assert task["status"] == "queued"
```

### Step 3: Create test_budget_enforcement.py

Test budget enforcement at checkpoint boundaries:

```python
class TestBudgetEnforcement:

    async def test_per_task_budget_pause(self, e2e):
        """Task exceeding per-task budget pauses with correct reason."""
        agent = e2e.ensure_agent(
            agent_id="budget-task-agent",
            budget_max_per_task=1,  # Very low budget (1 microdollar) to force immediate pause
        )

        task_id = e2e.submit_task(agent_id=agent["body"]["agent_id"])
        await e2e.wait_for_status(task_id, "paused", timeout=60)

        task = e2e.get_task(task_id)
        assert task["pause_reason"] == "budget_per_task"
        assert task["pause_details"]["recovery_mode"] == "manual_resume_after_budget_increase"
        assert task["resume_eligible_at"] is None

    async def test_hourly_budget_pause(self, e2e):
        """Agent exceeding hourly budget pauses tasks with auto-recovery."""
        agent = e2e.ensure_agent(
            agent_id="budget-hourly-agent",
            budget_max_per_hour=1,  # Very low hourly budget
        )

        task_id = e2e.submit_task(agent_id=agent["body"]["agent_id"])
        await e2e.wait_for_status(task_id, "paused", timeout=60)

        task = e2e.get_task(task_id)
        assert task["pause_reason"] == "budget_per_hour"
        assert task["pause_details"]["recovery_mode"] == "automatic_after_window_clears"
        assert task["resume_eligible_at"] is not None

    async def test_budget_precedence(self, e2e):
        """When both budgets exceeded, per-task takes precedence."""
        agent = e2e.ensure_agent(
            agent_id="budget-both-agent",
            budget_max_per_task=1,
            budget_max_per_hour=1,
        )

        task_id = e2e.submit_task(agent_id=agent["body"]["agent_id"])
        await e2e.wait_for_status(task_id, "paused", timeout=60)

        task = e2e.get_task(task_id)
        assert task["pause_reason"] == "budget_per_task"

    async def test_hourly_budget_blocks_new_claims(self, e2e):
        """Agent over hourly budget — new tasks remain queued."""
        # This test requires the agent to already have spent over budget in the last hour
        # Submit a task that triggers hourly budget pause, then submit another
        agent = e2e.ensure_agent(
            agent_id="hourly-block-agent",
            budget_max_per_hour=1,
        )

        task1 = e2e.submit_task(agent_id=agent["body"]["agent_id"])
        await e2e.wait_for_status(task1, "paused", timeout=60)

        task2 = e2e.submit_task(agent_id=agent["body"]["agent_id"])
        await asyncio.sleep(5)
        task2_status = e2e.get_task(task2)
        assert task2_status["status"] == "queued"  # Not claimed due to hourly budget

    async def test_task_paused_event_details(self, e2e):
        """Verify task_paused event has correct budget details schema."""
        agent = e2e.ensure_agent(
            agent_id="event-details-agent",
            budget_max_per_task=1,
        )

        task_id = e2e.submit_task(agent_id=agent["body"]["agent_id"])
        await e2e.wait_for_status(task_id, "paused", timeout=60)

        events = e2e.get_events(task_id)
        pause_events = [e for e in events if e["event_type"] == "task_paused"]
        assert len(pause_events) >= 1
        details = pause_events[0]["details"]
        assert details["pause_reason"] == "budget_per_task"
        assert "budget_max_per_task" in details
        assert "observed_task_cost_microdollars" in details
```

### Step 4: Create test_budget_resume.py

Test manual and automatic resume flows:

```python
class TestBudgetResume:

    async def test_manual_resume_after_budget_increase(self, e2e):
        """Per-task budget pause → increase budget → resume → task completes."""
        agent = e2e.ensure_agent(
            agent_id="resume-agent",
            budget_max_per_task=1,  # Force pause
        )

        task_id = e2e.submit_task(agent_id=agent["body"]["agent_id"])
        await e2e.wait_for_status(task_id, "paused", timeout=60)

        # Resume while still over budget → should fail with 409
        with pytest.raises(ApiError) as exc_info:
            e2e.resume_task(task_id)
        assert exc_info.value.status_code == 409

        # Increase budget
        e2e.api.update_agent(agent["body"]["agent_id"], budget_max_per_task=100_000_000)  # $100

        # Resume → should succeed
        result = e2e.resume_task(task_id)
        assert result["status"] == "queued"

        # Task should eventually complete
        await e2e.wait_for_status(task_id, "completed", timeout=60)

    async def test_resume_non_paused_task(self, e2e):
        """Resume on a running/queued task returns 409."""
        agent = e2e.ensure_agent(agent_id="resume-wrong-state")
        task_id = e2e.submit_task(agent_id=agent["body"]["agent_id"])

        with pytest.raises(ApiError) as exc_info:
            e2e.resume_task(task_id)
        assert exc_info.value.status_code == 409

    async def test_resume_nonexistent_task(self, e2e):
        """Resume on a nonexistent task returns 404."""
        with pytest.raises(ApiError) as exc_info:
            e2e.resume_task("00000000-0000-0000-0000-000000000000")
        assert exc_info.value.status_code == 404

    async def test_resume_disabled_agent_rejected(self, e2e):
        """Resume rejected when agent is disabled."""
        agent = e2e.ensure_agent(
            agent_id="resume-disabled-agent",
            budget_max_per_task=1,
        )
        task_id = e2e.submit_task(agent_id=agent["body"]["agent_id"])
        await e2e.wait_for_status(task_id, "paused", timeout=60)

        # Disable agent
        e2e.api.update_agent(agent["body"]["agent_id"], status="disabled")

        # Increase budget but agent is disabled
        e2e.api.update_agent(agent["body"]["agent_id"], budget_max_per_task=100_000_000)

        with pytest.raises(ApiError) as exc_info:
            e2e.resume_task(task_id)
        assert exc_info.value.status_code == 409

    async def test_task_resumed_event_details(self, e2e):
        """Verify task_resumed event has correct details after manual resume."""
        agent = e2e.ensure_agent(
            agent_id="resume-events-agent",
            budget_max_per_task=1,
        )

        task_id = e2e.submit_task(agent_id=agent["body"]["agent_id"])
        await e2e.wait_for_status(task_id, "paused", timeout=60)

        e2e.api.update_agent(agent["body"]["agent_id"], budget_max_per_task=100_000_000)
        e2e.resume_task(task_id)

        events = e2e.get_events(task_id)
        resume_events = [e for e in events if e["event_type"] == "task_resumed"]
        assert len(resume_events) >= 1
        details = resume_events[0]["details"]
        assert details["resume_trigger"] == "manual_operator_resume"
        assert "budget_max_per_task_at_resume" in details

    async def test_correct_event_ordering(self, e2e):
        """Events follow: submitted → claimed → paused → resumed → claimed → completed."""
        agent = e2e.ensure_agent(
            agent_id="event-order-agent",
            budget_max_per_task=1,
        )

        task_id = e2e.submit_task(agent_id=agent["body"]["agent_id"])
        await e2e.wait_for_status(task_id, "paused", timeout=60)

        e2e.api.update_agent(agent["body"]["agent_id"], budget_max_per_task=100_000_000)
        e2e.resume_task(task_id)
        await e2e.wait_for_status(task_id, "completed", timeout=60)

        events = e2e.get_events(task_id)
        event_types = [e["event_type"] for e in events]
        assert "task_submitted" in event_types
        assert "task_claimed" in event_types
        assert "task_paused" in event_types
        assert "task_resumed" in event_types
        assert "task_completed" in event_types

        # Verify ordering: paused comes after first claimed, resumed comes after paused
        claimed_idx = event_types.index("task_claimed")
        paused_idx = event_types.index("task_paused")
        resumed_idx = event_types.index("task_resumed")
        assert claimed_idx < paused_idx < resumed_idx
```

### Step 5: Add hourly auto-resume test (design doc requirement)

```python
async def test_hourly_auto_resume(self, e2e):
    """Hourly budget pause auto-recovers after rolling window clears.
    This test may need DB manipulation to simulate time passing."""
    agent = e2e.ensure_agent(
        agent_id="auto-resume-agent",
        budget_max_per_hour=1,
    )
    agent_id = agent["body"]["agent_id"]

    task_id = e2e.submit_task(agent_id=agent_id)
    await e2e.wait_for_status(task_id, "paused", timeout=60)

    task = e2e.get_task(task_id)
    assert task["pause_reason"] == "budget_per_hour"

    # Simulate time passing: delete old cost ledger entries and
    # update resume_eligible_at to the past
    await e2e.db.execute(
        "DELETE FROM agent_cost_ledger WHERE tenant_id = $1 AND agent_id = $2",
        e2e.tenant_id, agent_id
    )
    await e2e.db.execute(
        "UPDATE tasks SET resume_eligible_at = NOW() - INTERVAL '1 minute' WHERE task_id = $1::uuid",
        task_id
    )
    await e2e.db.execute(
        "UPDATE agent_runtime_state SET hour_window_cost_microdollars = 0 WHERE tenant_id = $1 AND agent_id = $2",
        e2e.tenant_id, agent_id
    )

    # Wait for reaper cycle to pick up the auto-recovery
    await e2e.wait_for_status(task_id, ["queued", "running", "completed"], timeout=120)

    # Verify task_resumed event was emitted
    events = e2e.get_events(task_id)
    resume_events = [e for e in events if e["event_type"] == "task_resumed"]
    assert len(resume_events) >= 1
    assert resume_events[0]["details"]["resume_trigger"] == "automatic_hourly_recovery"
```

### Step 6: Add agent budget CRUD tests

Add tests to the existing `test_agents.py` or a new file:

```python
async def test_create_agent_with_budget_fields(self, e2e):
    """Agent created with custom budget/concurrency settings."""
    resp = e2e.api.create_agent(
        agent_id="budget-crud-agent",
        max_concurrent_tasks=3,
        budget_max_per_task=1000000,
        budget_max_per_hour=10000000,
    )
    agent = resp["body"]
    assert agent["max_concurrent_tasks"] == 3
    assert agent["budget_max_per_task"] == 1000000
    assert agent["budget_max_per_hour"] == 10000000

async def test_create_agent_defaults(self, e2e):
    """Agent created without budget fields gets defaults."""
    resp = e2e.api.create_agent(agent_id="default-budget-agent")
    agent = resp["body"]
    assert agent["max_concurrent_tasks"] == 5
    assert agent["budget_max_per_task"] == 500000
    assert agent["budget_max_per_hour"] == 5000000

async def test_update_agent_budget(self, e2e):
    """Agent budget fields can be updated."""
    e2e.api.create_agent(agent_id="update-budget-agent")
    resp = e2e.api.update_agent("update-budget-agent", budget_max_per_task=2000000)
    updated = resp["body"]
    assert updated["budget_max_per_task"] == 2000000

async def test_task_list_pause_reason_filter(self, e2e):
    """Task list filters by pause_reason."""
    agent = e2e.ensure_agent(agent_id="filter-agent", budget_max_per_task=1)
    task_id = e2e.submit_task(agent_id=agent["body"]["agent_id"])
    await e2e.wait_for_status(task_id, "paused", timeout=60)

    # Filter by pause_reason
    resp = e2e.api.list_tasks(status="paused", pause_reason="budget_per_task")
    tasks = resp["body"]["tasks"]
    assert any(t["task_id"] == task_id for t in tasks)

    # Filter by different pause_reason — should not include our task
    resp = e2e.api.list_tasks(status="paused", pause_reason="budget_per_hour")
    tasks = resp["body"]["tasks"]
    assert not any(t["task_id"] == task_id for t in tasks)
```

## Acceptance Criteria

- [ ] E2E helpers extended with `resume_task()` and budget-aware `ensure_agent()`
- [ ] Round-robin fairness: two agents' tasks are claimed in alternating order
- [ ] Concurrency cap: agent with `max_concurrent_tasks=1` has at most 1 running task
- [ ] Disabled agent: tasks remain queued
- [ ] Per-task budget: task pauses with `budget_per_task` reason and correct details
- [ ] Hourly budget: task pauses with `budget_per_hour` reason, `resume_eligible_at` set
- [ ] Budget precedence: per-task wins when both exceeded
- [ ] Hourly budget blocks new claims for the over-budget agent
- [ ] `task_paused` event has correct budget details schema
- [ ] Manual resume after budget increase: task resumes and completes
- [ ] Resume while still over budget: 409
- [ ] Resume non-paused task: 409
- [ ] Resume nonexistent task: 404
- [ ] Resume disabled agent: 409
- [ ] `task_resumed` event has correct details
- [ ] Event ordering: submitted → claimed → paused → resumed → claimed → completed
- [ ] Hourly auto-resume: task paused for hourly budget auto-recovers after window clears
- [ ] Agent CRUD with budget fields: create with custom values, verify defaults, update
- [ ] Task list `pause_reason` filter works correctly
- [ ] `_do_clean()` updated to clean `agent_runtime_state` and `agent_cost_ledger` tables

## Testing Requirements

- All tests must be runnable via the existing integration test harness (`pytest`)
- Tests must be independent and idempotent
- Tests should clean up created agents and tasks (use unique agent_ids per test)
- Timing-sensitive tests should use generous timeouts with polling

## Constraints and Guardrails

- Do not modify application code — this task only adds tests.
- Use the existing E2E helpers and patterns. Extend them as needed but keep extensions backward-compatible.
- Tests for auto-recovery timing may be flaky if the reaper cycle is slow — use direct reaper invocation if available, or generous timeouts.
- **IMPORTANT — Cost feasibility:** Using `budget_max_per_task=1` to force immediate pause depends on the mock LLM producing non-zero cost metadata. If `DynamicChatProvider` mock responses have zero cost, budget tests will fail. Solutions: (a) configure the mock to include realistic `usage` metadata in `response_metadata`, (b) insert artificial cost entries directly into `agent_cost_ledger` via DB helper to simulate cost accrual, or (c) add a test-only cost injection hook to the executor. **Verify this works before writing all budget tests.**

## Assumptions

- All backend tasks (1-6) have been completed.
- The worker and API service are running in the test environment.
- The reaper is running on a regular cycle in the test environment.
- The test agent config produces tasks with non-zero LLM cost (at least 1 microdollar per step). **Verify this assumption first** — if mock LLM responses have zero cost, budget tests will need DB manipulation or mock cost injection.
- The existing test infrastructure (conftest, fixtures, cleanup) supports the new test files.
- `ApiError` from `helpers/api_client.py` is used for error assertions (NOT `requests.HTTPError`).
- `E2EContext` sync methods (`ensure_agent`, `submit_task`, `get_task`, `get_events`, `resume_task`) are NOT awaited. Only `wait_for_status` and other explicitly async methods are awaited.

<!-- AGENT_TASK_END: task-8-integration-tests.md -->
