"""Integration tests for the Human-in-the-Loop input request flow.

These tests exercise the full pipeline: task submission -> worker execution
-> request_human_input tool call -> waiting_for_input pause -> human respond
-> worker resume -> completion.

Prerequisites: Tasks 1-5 of Phase 2 Track 2 must be implemented (DB schema,
event service, HITL API endpoints, worker interrupt handling, event integration).
"""

import json
from datetime import datetime, timezone

import pytest

from helpers.mock_llm import simple_response
from langchain_core.messages import AIMessage, ToolCall
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Mock LLM helpers specific to HITL flows
# ---------------------------------------------------------------------------

def _new_mock() -> MagicMock:
    mock = MagicMock()
    mock.bind_tools.return_value = mock
    return mock


def request_human_input_then_answer(
    prompt: str = "What color do you prefer?",
    final_answer: str = "The user said blue.",
) -> MagicMock:
    """Mock LLM that first calls request_human_input, then gives a final answer.

    The first invocation returns a tool call to request_human_input.
    The second invocation (after the interrupt round-trip) returns the final answer.
    """
    tool_call_msg = AIMessage(
        content="",
        tool_calls=[
            ToolCall(
                name="request_human_input",
                args={"prompt": prompt},
                id="call_hitl_input",
            )
        ],
    )
    final_msg = AIMessage(content=final_answer)
    mock = _new_mock()
    mock.ainvoke = AsyncMock(side_effect=[tool_call_msg, final_msg])
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_request_respond_completion(e2e):
    """Full HITL input flow: submit -> waiting_for_input -> respond -> completed."""
    e2e.use_llm(
        request_human_input_then_answer(
            prompt="What color do you prefer?",
            final_answer="The user chose blue.",
        )
    )
    await e2e.start_worker("e2e-hitl-input")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant that asks the user for input.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["request_human_input"],
    })
    task_id = e2e.submit_task(input="Ask the user what color they prefer")

    # Wait for the task to reach waiting_for_input
    waiting = await e2e.wait_for_status(task_id, "waiting_for_input", timeout=30.0)
    assert waiting["status"] == "waiting_for_input"

    # Assert pending_input_prompt is set
    assert waiting.get("pending_input_prompt") is not None
    assert len(waiting["pending_input_prompt"]) > 0

    # Assert human_input_timeout_at is set (should be approximately 24h from now)
    timeout_at = waiting.get("human_input_timeout_at")
    assert timeout_at is not None

    # Assert lease is released while paused
    row = await e2e.db.fetch_task_columns(task_id, "lease_owner", "lease_expiry")
    assert row is not None
    assert row["lease_owner"] is None

    # Respond to the input request
    respond_result = e2e.api.respond_to_task(task_id, "blue")
    assert respond_result["status_code"] == 200

    # Wait for completion
    completed = await e2e.wait_for_status(task_id, "completed", timeout=30.0)
    assert completed["status"] == "completed"
    assert completed["output"] is not None


@pytest.mark.asyncio
async def test_input_request_cancel(e2e):
    """Cancel a task that is waiting for human input."""
    e2e.use_llm(
        request_human_input_then_answer(
            prompt="What is your name?",
            final_answer="Hello!",
        )
    )
    await e2e.start_worker("e2e-hitl-cancel-input")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["request_human_input"],
    })
    task_id = e2e.submit_task(input="Ask the user their name")

    # Wait for waiting_for_input
    waiting = await e2e.wait_for_status(task_id, "waiting_for_input", timeout=30.0)
    assert waiting["status"] == "waiting_for_input"

    # Cancel from waiting state
    cancel = e2e.api.cancel_task(task_id)["body"]
    assert cancel["status"] == "dead_letter"
    assert cancel["dead_letter_reason"] == "cancelled_by_user"

    # Verify in DB
    row = await e2e.db.fetch_task_columns(
        task_id, "status", "dead_letter_reason", "lease_owner",
        "pending_input_prompt", "human_input_timeout_at",
    )
    assert row is not None
    assert row["status"] == "dead_letter"
    assert row["dead_letter_reason"] == "cancelled_by_user"
    assert row["lease_owner"] is None
    # HITL fields should be cleared on cancel
    assert row["pending_input_prompt"] is None
    assert row["human_input_timeout_at"] is None


@pytest.mark.asyncio
async def test_input_request_timeout_via_db(e2e):
    """Test HITL timeout by directly setting human_input_timeout_at to the past.

    This test manipulates the DB directly to simulate timeout expiry, then
    waits for the reaper to pick it up. If the reaper cycle is too slow,
    this test may need to be skipped or adjusted.
    """
    e2e.use_llm(
        request_human_input_then_answer(
            prompt="Pick a number",
            final_answer="ok",
        )
    )
    await e2e.start_worker("e2e-hitl-timeout")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["request_human_input"],
    })
    task_id = e2e.submit_task(input="Ask the user to pick a number")

    # Wait for waiting_for_input
    waiting = await e2e.wait_for_status(task_id, "waiting_for_input", timeout=30.0)
    assert waiting["status"] == "waiting_for_input"

    # Set timeout to the past so the reaper will pick it up
    await e2e.db.execute(
        "UPDATE tasks SET human_input_timeout_at = NOW() - INTERVAL '1 minute' WHERE task_id = $1::uuid",
        task_id,
    )

    # Wait for the reaper to detect the timeout and dead-letter the task.
    # The reaper interval in tests is ~5s, so we give it up to 30s.
    dead = await e2e.wait_for_status(task_id, "dead_letter", timeout=30.0)
    assert dead["status"] == "dead_letter"
    assert dead["dead_letter_reason"] == "human_input_timeout"


@pytest.mark.asyncio
async def test_wrong_state_approve_returns_409(e2e):
    """Calling approve on a task not in waiting_for_approval returns 409."""
    e2e.use_llm(simple_response("done"))
    await e2e.start_worker("e2e-hitl-wrong-state-approve")

    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Quick task")

    # Wait for terminal state
    await e2e.wait_for_statuses(task_id, {"completed", "dead_letter"}, timeout=20.0)

    # Approve should fail with 409
    result = e2e.api.approve_task_raw(task_id)
    assert result["status_code"] == 409


@pytest.mark.asyncio
async def test_wrong_state_respond_returns_409(e2e):
    """Calling respond on a task not in waiting_for_input returns 409."""
    e2e.use_llm(simple_response("done"))
    await e2e.start_worker("e2e-hitl-wrong-state-respond")

    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Quick task")

    # Wait for terminal state
    await e2e.wait_for_statuses(task_id, {"completed", "dead_letter"}, timeout=20.0)

    # Respond should fail with 409
    result = e2e.api.respond_to_task_raw(task_id, "test message")
    assert result["status_code"] == 409
