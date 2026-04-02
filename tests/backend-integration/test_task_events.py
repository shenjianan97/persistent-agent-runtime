"""Integration tests for the task events audit trail.

These tests verify that lifecycle transitions produce the correct sequence
of task_events records accessible via GET /v1/tasks/{id}/events.

Prerequisites: Tasks 1-2 and 5 of Phase 2 Track 2 must be implemented
(DB schema, event service, event integration from all state transitions).
"""

import pytest

from helpers.mock_llm import simple_response
from langchain_core.messages import AIMessage, ToolCall
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Mock LLM helpers
# ---------------------------------------------------------------------------

def _new_mock() -> MagicMock:
    mock = MagicMock()
    mock.bind_tools.return_value = mock
    return mock


def request_human_input_then_answer(
    prompt: str = "What color?",
    final_answer: str = "The user said blue.",
) -> MagicMock:
    tool_call_msg = AIMessage(
        content="",
        tool_calls=[
            ToolCall(
                name="request_human_input",
                args={"prompt": prompt},
                id="call_hitl_events",
            )
        ],
    )
    final_msg = AIMessage(content=final_answer)
    mock = _new_mock()
    mock.ainvoke = AsyncMock(side_effect=[tool_call_msg, final_msg])
    return mock


# ---------------------------------------------------------------------------
# Helper to extract event types from the events response
# ---------------------------------------------------------------------------

def _event_types(events_body: dict) -> list[str]:
    """Extract ordered list of event_type values from an events API response."""
    events = events_body.get("events", [])
    return [e["event_type"] for e in events]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_lifecycle_event_sequence(e2e):
    """Simple task lifecycle produces: task_submitted, task_claimed, task_completed."""
    e2e.use_llm(simple_response("done"))
    await e2e.start_worker("e2e-events-lifecycle")

    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Simple event tracking")

    await e2e.wait_for_status(task_id, "completed", timeout=20.0)

    events_resp = e2e.api.get_task_events(task_id)
    assert events_resp["status_code"] == 200
    types = _event_types(events_resp["body"])

    # The sequence must contain these events in order
    assert "task_submitted" in types
    assert "task_claimed" in types
    assert "task_completed" in types

    # Verify ordering: submitted before claimed before completed
    idx_submitted = types.index("task_submitted")
    idx_claimed = types.index("task_claimed")
    idx_completed = types.index("task_completed")
    assert idx_submitted < idx_claimed < idx_completed

    # Verify event fields
    events = events_resp["body"]["events"]
    submitted = next(e for e in events if e["event_type"] == "task_submitted")
    assert submitted["status_after"] == "queued"
    assert submitted["task_id"] == task_id

    claimed = next(e for e in events if e["event_type"] == "task_claimed")
    assert claimed["status_before"] == "queued"
    assert claimed["status_after"] == "running"

    completed_evt = next(e for e in events if e["event_type"] == "task_completed")
    assert completed_evt["status_before"] == "running"
    assert completed_evt["status_after"] == "completed"


@pytest.mark.asyncio
async def test_cancel_event(e2e):
    """Cancelling a task produces a task_cancelled event."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Cancel event test")

    # Cancel immediately (while queued)
    cancel = e2e.api.cancel_task(task_id)["body"]
    assert cancel["status"] == "dead_letter"

    events_resp = e2e.api.get_task_events(task_id)
    assert events_resp["status_code"] == 200
    types = _event_types(events_resp["body"])

    assert "task_cancelled" in types
    cancelled_evt = next(
        e for e in events_resp["body"]["events"] if e["event_type"] == "task_cancelled"
    )
    assert cancelled_evt["status_after"] == "dead_letter"


@pytest.mark.asyncio
async def test_redrive_event(e2e):
    """Redriving a dead-lettered task produces a task_redriven event."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Redrive event test")

    # Cancel to dead-letter
    e2e.api.cancel_task(task_id)

    # Redrive
    redrive = e2e.api.redrive_task(task_id)["body"]
    assert redrive["status"] == "queued"

    events_resp = e2e.api.get_task_events(task_id)
    assert events_resp["status_code"] == 200
    types = _event_types(events_resp["body"])

    assert "task_redriven" in types
    redriven_evt = next(
        e for e in events_resp["body"]["events"] if e["event_type"] == "task_redriven"
    )
    assert redriven_evt["status_before"] == "dead_letter"
    assert redriven_evt["status_after"] == "queued"


@pytest.mark.asyncio
async def test_hitl_events(e2e):
    """HITL input flow produces input_requested and input_received events."""
    e2e.use_llm(
        request_human_input_then_answer(
            prompt="What color?",
            final_answer="The user chose blue.",
        )
    )
    await e2e.start_worker("e2e-events-hitl")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["request_human_input"],
    })
    task_id = e2e.submit_task(input="Ask the user what color")

    # Wait for input request
    await e2e.wait_for_status(task_id, "waiting_for_input", timeout=30.0)

    # Respond
    e2e.api.respond_to_task(task_id, "blue")

    # Wait for completion
    await e2e.wait_for_status(task_id, "completed", timeout=30.0)

    events_resp = e2e.api.get_task_events(task_id)
    assert events_resp["status_code"] == 200
    types = _event_types(events_resp["body"])

    # Verify the HITL-specific events are present
    assert "task_submitted" in types
    assert "task_claimed" in types
    assert "task_input_requested" in types
    assert "task_input_received" in types
    assert "task_completed" in types

    # Verify ordering
    idx_input_requested = types.index("task_input_requested")
    idx_input_received = types.index("task_input_received")
    assert idx_input_requested < idx_input_received

    # Verify the input_received event has a second claim after resume
    # (the task goes back to queued, then gets claimed again)
    claimed_indices = [i for i, t in enumerate(types) if t == "task_claimed"]
    assert len(claimed_indices) >= 2, (
        f"Expected at least 2 task_claimed events (initial + resume), got {len(claimed_indices)}: {types}"
    )
    # Second claim should be after input_received
    assert claimed_indices[-1] > idx_input_received


@pytest.mark.asyncio
async def test_fresh_task_has_submitted_event(e2e):
    """A freshly submitted task (before worker claims) has at least task_submitted."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Fresh event check")

    events_resp = e2e.api.get_task_events(task_id)
    assert events_resp["status_code"] == 200
    types = _event_types(events_resp["body"])

    assert "task_submitted" in types


@pytest.mark.asyncio
async def test_events_limit_parameter(e2e):
    """The limit parameter restricts the number of returned events."""
    e2e.ensure_agent()
    task_id = e2e.submit_task(input="Limit test")

    # Generate multiple events: submit -> cancel -> redrive
    e2e.api.cancel_task(task_id)
    e2e.api.redrive_task(task_id)

    # Get all events first to confirm there are more than 2
    all_events = e2e.api.get_task_events(task_id)
    all_types = _event_types(all_events["body"])
    assert len(all_types) >= 3, f"Expected at least 3 events, got {len(all_types)}: {all_types}"

    # Now request with limit=2
    limited = e2e.api.get_task_events(task_id, limit=2)
    assert limited["status_code"] == 200
    limited_events = limited["body"]["events"]
    assert len(limited_events) == 2

    # Events should be oldest first
    assert limited_events[0]["event_type"] == all_types[0]
    assert limited_events[1]["event_type"] == all_types[1]


@pytest.mark.asyncio
async def test_events_nonexistent_task_returns_404(e2e):
    """Getting events for a nonexistent task returns 404."""
    result = e2e.api.get_task_events(
        "00000000-0000-0000-0000-000000000000",
        raise_for_status=False,
    )
    assert result["status_code"] == 404
