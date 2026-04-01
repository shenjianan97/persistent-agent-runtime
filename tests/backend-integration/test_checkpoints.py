import pytest

from helpers.mock_llm import calculator_tool_call


@pytest.mark.asyncio
async def test_3_13_checkpoint_history_verification(e2e):
    """3.13 Verify checkpoint sequencing, node metadata, and cost aggregation fields."""
    e2e.use_llm(calculator_tool_call(expression="3*7", final_answer="21"))
    await e2e.start_worker("e2e-checkpoints")

    e2e.ensure_agent()
    task_id = e2e.submit_task(input="calc")
    task = await e2e.wait_for_status(task_id, "completed", timeout=20.0)

    checkpoints = e2e.get_checkpoints(task_id)
    assert checkpoints
    assert [cp["step_number"] for cp in checkpoints] == list(range(1, len(checkpoints) + 1))
    assert all(cp["worker_id"] for cp in checkpoints)
    assert all(cp["node_name"] for cp in checkpoints)

    total_cost = sum(cp.get("cost_microdollars", 0) for cp in checkpoints)
    assert task["total_cost_microdollars"] >= 0
    assert total_cost == task["total_cost_microdollars"]
