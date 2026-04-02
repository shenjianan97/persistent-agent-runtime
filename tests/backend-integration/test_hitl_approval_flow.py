"""Integration tests for the Human-in-the-Loop approval/rejection flow.

Since approval gates for non-idempotent tool calls are not implemented until
Track 5, these tests validate the approve/reject API contract by using direct
DB manipulation to put tasks into waiting_for_approval state.

Prerequisites: Tasks 1-3 of Phase 2 Track 2 must be implemented (DB schema,
event service, HITL API endpoints).
"""

import json

import pytest

from helpers.mock_llm import simple_response


@pytest.mark.asyncio
async def test_approve_transitions_to_queued(e2e):
    """Approve a task in waiting_for_approval -> transitions back to queued."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Approval test")

    # Directly set the task to waiting_for_approval with a mock pending action
    # (no worker needed — we skip the execution step entirely)
    mock_action = json.dumps({
        "tool_name": "dangerous_tool",
        "tool_args": {"target": "production"},
    })
    await e2e.db.execute(
        """
        UPDATE tasks
        SET status = 'waiting_for_approval',
            pending_approval_action = $1::jsonb,
            human_input_timeout_at = NOW() + INTERVAL '24 hours',
            lease_owner = NULL,
            lease_expiry = NULL,
            updated_at = NOW()
        WHERE task_id = $2::uuid
        """,
        mock_action,
        task_id,
    )

    # Approve
    result = e2e.api.approve_task(task_id)
    assert result["status_code"] == 200

    # Verify task is back to queued
    task = e2e.get_task(task_id)
    assert task["status"] == "queued"

    # Verify HITL fields are cleared
    row = await e2e.db.fetch_task_columns(
        task_id, "status", "pending_approval_action", "human_input_timeout_at",
        "lease_owner", "lease_expiry",
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["pending_approval_action"] is None
    assert row["human_input_timeout_at"] is None
    assert row["lease_owner"] is None


@pytest.mark.asyncio
async def test_reject_transitions_to_queued(e2e):
    """Reject a task in waiting_for_approval -> transitions back to queued."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Rejection test")

    # Set to waiting_for_approval directly (no worker needed)
    mock_action = json.dumps({
        "tool_name": "dangerous_tool",
        "tool_args": {"target": "production"},
    })
    await e2e.db.execute(
        """
        UPDATE tasks
        SET status = 'waiting_for_approval',
            pending_approval_action = $1::jsonb,
            human_input_timeout_at = NOW() + INTERVAL '24 hours',
            lease_owner = NULL,
            lease_expiry = NULL,
            updated_at = NOW()
        WHERE task_id = $2::uuid
        """,
        mock_action,
        task_id,
    )

    # Reject
    result = e2e.api.reject_task(task_id, "Not safe to execute in production")
    assert result["status_code"] == 200

    # Verify task transitions back to queued
    task = e2e.get_task(task_id)
    assert task["status"] == "queued"

    # Verify HITL fields are cleared
    row = await e2e.db.fetch_task_columns(
        task_id, "status", "pending_approval_action", "human_input_timeout_at",
        "lease_owner",
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["pending_approval_action"] is None
    assert row["human_input_timeout_at"] is None
    assert row["lease_owner"] is None


@pytest.mark.asyncio
async def test_approve_wrong_state_returns_409(e2e):
    """Calling approve on a task not in waiting_for_approval returns 409."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Not waiting for approval")

    # Task is queued — approve should fail
    result = e2e.api.approve_task_raw(task_id)
    assert result["status_code"] == 409


@pytest.mark.asyncio
async def test_approve_nonexistent_returns_404(e2e):
    """Calling approve on a nonexistent task returns 404."""
    result = e2e.api.approve_task_raw("00000000-0000-0000-0000-000000000000")
    assert result["status_code"] == 404


@pytest.mark.asyncio
async def test_reject_wrong_state_returns_409(e2e):
    """Calling reject on a task not in waiting_for_approval returns 409."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Not waiting for approval")

    result = e2e.api.reject_task_raw(task_id, "should fail")
    assert result["status_code"] == 409


@pytest.mark.asyncio
async def test_reject_nonexistent_returns_404(e2e):
    """Calling reject on a nonexistent task returns 404."""
    result = e2e.api.reject_task_raw(
        "00000000-0000-0000-0000-000000000000", "reason"
    )
    assert result["status_code"] == 404


@pytest.mark.asyncio
async def test_respond_on_waiting_for_approval_returns_409(e2e):
    """Calling respond on a task in waiting_for_approval (not waiting_for_input) returns 409."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Mismatched state test")

    # Directly set to waiting_for_approval
    mock_action = json.dumps({"tool_name": "test_tool", "tool_args": {}})
    await e2e.db.execute(
        """
        UPDATE tasks
        SET status = 'waiting_for_approval',
            pending_approval_action = $1::jsonb,
            lease_owner = NULL,
            lease_expiry = NULL,
            updated_at = NOW()
        WHERE task_id = $2::uuid
        """,
        mock_action,
        task_id,
    )

    result = e2e.api.respond_to_task_raw(task_id, "wrong endpoint")
    assert result["status_code"] == 409
