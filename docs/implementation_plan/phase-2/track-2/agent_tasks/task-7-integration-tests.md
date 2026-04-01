<!-- AGENT_TASK_START: task-7-integration-tests.md -->

# Task 7 — Integration Tests

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design/phase-2/PHASE2_MULTI_AGENT.md` — Sections 5, 7, 8 for expected behavior
2. `tests/backend-integration/` — existing test files for patterns and helpers
3. `tests/backend-integration/helpers/api_client.py` — existing API client helper
4. `services/worker-service/tools/definitions.py` — to understand the `request_human_input` tool (Task 4 output)
5. `docs/implementation_plan/phase-2/track-2/plan.md` — full Track 2 context

**CRITICAL POST-WORK:** After completing this task, update the status in `docs/implementation_plan/phase-2/track-2/progress.md` to "Done".

## Context

This task validates the full Track 2 backend pipeline end-to-end: human-in-the-loop workflows (approval, rejection, input response, timeout) and task event recording. The tests exercise the complete flow from task submission through worker execution, HITL pause/resume, and event audit trail verification.

## Task-Specific Shared Contract

- Tests use the existing integration test infrastructure (pytest, test database, API client helper).
- The `request_human_input` tool (Task 4) triggers the `waiting_for_input` pause state.
- Approval gates for tool calls are not yet implemented (Track 5) — only input-request flows are testable end-to-end.
- The reaper runs on a periodic scan (~30s). Tests that rely on reaper behavior may need to either wait for a scan cycle or directly invoke the reaper's scan method.
- Event assertions should verify the event sequence, not exact timestamps.

## Affected Component

- **Service/Module:** Integration Tests
- **File paths:**
  - `tests/backend-integration/test_hitl_approval_flow.py` (new)
  - `tests/backend-integration/test_hitl_input_flow.py` (new)
  - `tests/backend-integration/test_task_events.py` (new)
  - `tests/backend-integration/helpers/api_client.py` (modify — add new endpoint methods)
- **Change type:** new code + modification

## Dependencies

- **Must complete first:** Task 1 (schema), Task 2 (event service), Task 3 (HITL API), Task 4 (worker interrupt), Task 5 (event integration)
- **Provides output to:** None (terminal task)
- **Shared interfaces/contracts:** API contracts from Tasks 2, 3; worker behavior from Tasks 4, 5

## Implementation Specification

### Step 1: Update API client helper

In `tests/backend-integration/helpers/api_client.py`, add methods using the existing `_request()` helper:

```python
def approve_task(self, task_id: str) -> dict:
    """POST /v1/tasks/{task_id}/approve"""
    return self._request("POST", f"/tasks/{task_id}/approve")

def reject_task(self, task_id: str, reason: str) -> dict:
    """POST /v1/tasks/{task_id}/reject"""
    return self._request("POST", f"/tasks/{task_id}/reject", {"reason": reason})

def respond_to_task(self, task_id: str, message: str) -> dict:
    """POST /v1/tasks/{task_id}/respond"""
    return self._request("POST", f"/tasks/{task_id}/respond", {"message": message})

def get_task_events(self, task_id: str, limit: int = 100) -> dict:
    """GET /v1/tasks/{task_id}/events"""
    return self._request("GET", f"/tasks/{task_id}/events?limit={limit}")
```

Also add error-returning variants for wrong-state testing:

```python
def approve_task_raw(self, task_id: str) -> dict:
    """POST /v1/tasks/{task_id}/approve — returns status/body for status-code checks"""
    return self._request("POST", f"/tasks/{task_id}/approve", raise_for_status=False)

def respond_to_task_raw(self, task_id: str, message: str) -> dict:
    """POST /v1/tasks/{task_id}/respond — returns status/body for status-code checks"""
    return self._request(
        "POST",
        f"/tasks/{task_id}/respond",
        {"message": message},
        raise_for_status=False,
    )
```

### Step 2: Create test_hitl_input_flow.py

This is the primary HITL test — it uses the `request_human_input` tool to trigger the full flow.

**Test: Input request → respond → completion**
1. Create an agent with `allowed_tools: ["request_human_input"]`
2. Submit a task with input that will cause the agent to call `request_human_input` (e.g., "Ask the user what color they prefer")
3. Poll task status until it reaches `waiting_for_input` (with timeout)
4. Assert `pending_input_prompt` is set on the task detail response
5. Assert the task has released its lease while paused
6. Assert `human_input_timeout_at` is set (approximately 24 hours from now)
7. Call `respond_to_task(task_id, "blue")`
8. Optionally observe the task return to `queued`
9. Poll task status until it reaches `completed` (with timeout)
10. Assert task output contains the agent's response incorporating the human input

**Test: Input request → cancel**
1. Submit task that triggers `request_human_input`
2. Wait for `waiting_for_input`
3. Call `cancel_task(task_id)`
4. Assert task status is `dead_letter` with reason `cancelled_by_user`

**Test: Input request → timeout**
(This test may be slow or require test infrastructure to accelerate the reaper timeout. Options:)
- Option A: Use a very short `human_input_timeout_at` by directly updating the DB in the test
- Option B: Skip this test if the reaper's scan interval makes it impractical
- Option C: Add a test-only endpoint to trigger a reaper scan

If feasible:
1. Submit task that triggers `request_human_input`
2. Wait for `waiting_for_input`
3. Directly UPDATE `human_input_timeout_at` to a past timestamp in the test DB
4. Wait for reaper cycle (or trigger scan)
5. Assert task status is `dead_letter` with reason `human_input_timeout`

**Test: Wrong-state errors**
1. Submit a task, wait for `running` or `completed`
2. Call `approve_task_raw(task_id)` — assert status code 409
3. Call `respond_to_task_raw(task_id, "test")` — assert status code 409

### Step 3: Create test_hitl_approval_flow.py

Since approval gates for non-idempotent tools are not implemented until Track 5, this test validates the approve/reject API contract at the payload-and-requeue level. It may use direct DB manipulation to put a task into `waiting_for_approval` state for testing.

**Test: Approve transition**
1. Submit a task, wait for it to be claimed (`running`)
2. Directly UPDATE task status to `waiting_for_approval` with a mock `pending_approval_action` (test helper)
3. Call `approve_task(task_id)`
4. Assert `pending_approval_action` is cleared
5. Assert `human_response` stores the documented approval payload
6. Assert the task transitions back to `queued` and has no active lease

**Test: Reject transition**
1. Set up task in `waiting_for_approval` (same approach)
2. Call `reject_task(task_id, "Not safe to execute")`
3. Assert `pending_approval_action` is cleared
4. Assert `human_response` stores the documented rejection payload (via task detail or DB check)
5. Assert the task transitions back to `queued` and has no active lease

**Test: Approve on wrong state → 409**
1. Submit a task in `queued` state
2. Call `approve_task_raw(task_id)` → assert 409

**Test: Approve on nonexistent → 404**
1. Call `approve_task_raw("00000000-0000-0000-0000-000000000000")` → assert 404

### Step 4: Create test_task_events.py

**Test: Complete lifecycle event sequence**
1. Submit a task (simple, no HITL)
2. Wait for `completed`
3. Call `get_task_events(task_id)`
4. Assert events contain (in order):
   - `task_submitted` (status_after: queued)
   - `task_claimed` (status_before: queued, status_after: running)
   - `task_completed` (status_before: running, status_after: completed)

**Test: Cancel event**
1. Submit a task
2. Cancel immediately
3. Get events
4. Assert `task_cancelled` event present with status_after `dead_letter`

**Test: Redrive event**
1. Get a dead-lettered task (or create one via cancel)
2. Redrive it
3. Get events
4. Assert `task_redriven` event present with status_before `dead_letter`, status_after `queued`

**Test: HITL events**
1. Submit task with `request_human_input`
2. Wait for `waiting_for_input`
3. Respond
4. Wait for completion
5. Get events
6. Assert sequence includes: `task_submitted`, `task_claimed`, `task_input_requested`, `task_input_received`, `task_claimed` (second claim after resume), `task_completed`

**Test: Empty events for fresh task**
1. Submit a task
2. Immediately get events (before worker claims)
3. Assert at least `task_submitted` event present

**Test: Events limit parameter**
1. Create a task with multiple events (submit, cancel, redrive, complete)
2. Call `get_task_events(task_id, limit=2)`
3. Assert only 2 events returned (oldest first)

## Acceptance Criteria

- [ ] All HITL input flow tests pass (input request → respond → completion)
- [ ] Wrong-state error tests return correct HTTP status codes (409, 404)
- [ ] Cancel from waiting state works correctly
- [ ] Event sequence tests verify correct chronological order
- [ ] Events include correct status_before and status_after values
- [ ] Events limit parameter works correctly
- [ ] Redrive event appears in the timeline
- [ ] All tests run within the existing integration test harness

## Testing Requirements

- **Execution:** `pytest tests/backend-integration/` with all services running
- **Prerequisites:** API service, worker service, and PostgreSQL must be running
- **Timeout:** Individual tests should complete within 60 seconds (account for worker poll intervals)
- **Isolation:** Each test should create its own agent and task to avoid interference

## Constraints and Guardrails

- Follow existing test patterns and conventions from the `tests/backend-integration/` directory.
- Do not modify application code — this task is test-only.
- Use polling with timeouts (not sleep) to wait for async state transitions.
- Tests that require direct DB manipulation should clearly document why (e.g., approval gates not yet implemented).
- Do not test Console UI — this task covers backend integration only.

## Assumptions

- All backend tasks (1-5) have been completed and services are running.
- The worker is configured with `request_human_input` in the tool set.
- The existing test infrastructure handles database cleanup between tests.
- The agent used for HITL tests has `request_human_input` in its `allowed_tools`.
- Worker poll intervals are short enough that tasks are claimed within a few seconds.

<!-- AGENT_TASK_END: task-7-integration-tests.md -->
