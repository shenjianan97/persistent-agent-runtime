import pytest

from helpers.mock_llm import calculator_tool_call, simple_response


@pytest.mark.asyncio
async def test_3_1_happy_path_submit_execute_complete(e2e):
    """3.1 Happy path: queued -> running -> completed with checkpoints."""
    e2e.use_llm(calculator_tool_call(expression="5*5", final_answer="The result is 25"))
    await e2e.start_worker("e2e-happy-worker")

    e2e.ensure_agent()
    task_id = e2e.submit_task(
        input="What is 5*5?",
    )

    completed = await e2e.wait_for_status(task_id, "completed", timeout=20.0)
    assert "25" in str(completed["output"]["result"])

    checkpoints = e2e.get_checkpoints(task_id)
    assert checkpoints
    assert [cp["step_number"] for cp in checkpoints] == list(range(1, len(checkpoints) + 1))
    assert all(cp["worker_id"] for cp in checkpoints)
    assert completed["total_cost_microdollars"] >= 0

    row = await e2e.db.fetch_task_columns(task_id, "status", "lease_owner", "version")
    assert row is not None
    assert row["status"] == "completed"
    assert row["lease_owner"] is None
    assert row["version"] >= 3


@pytest.mark.asyncio
async def test_3_2_simple_completion_no_tools(e2e):
    """3.2 No-tool path: agent -> END."""
    e2e.use_llm(simple_response("Hello there!"))
    await e2e.start_worker("e2e-simple-worker")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": []
    })
    task_id = e2e.submit_task(input="Say hello")
    completed = await e2e.wait_for_status(task_id, "completed", timeout=20.0)

    assert completed["output"]["result"] == "Hello there!"
    assert e2e.get_checkpoints(task_id)
