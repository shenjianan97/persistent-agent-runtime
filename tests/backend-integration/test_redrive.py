import pytest

from helpers.mock_llm import always_fails, simple_response


@pytest.mark.asyncio
async def test_3_11_redrive_from_dead_letter(e2e):
    """3.11 Dead-lettered task can be redriven and completed."""
    e2e.use_llm(always_fails("400 Bad Request: invalid prompt"))
    await e2e.start_worker("e2e-redrive")

    e2e.ensure_agent()
    task_id = e2e.submit_task(input="first fail")
    dead = await e2e.wait_for_status(task_id, "dead_letter", timeout=20.0)
    assert dead["dead_letter_reason"] == "non_retryable_error"

    checkpoints_before = await e2e.db.checkpoint_count(task_id)

    e2e.use_llm(simple_response("success after redrive"))
    redrive = e2e.api.redrive_task(task_id)["body"]
    assert redrive["status"] == "queued"

    requeued = e2e.get_task(task_id)
    assert requeued["retry_count"] == 0
    assert requeued["dead_letter_reason"] is None

    done = await e2e.wait_for_status(task_id, "completed", timeout=25.0)
    assert done["status"] == "completed"
    assert await e2e.db.checkpoint_count(task_id) >= checkpoints_before


@pytest.mark.asyncio
async def test_3_12_dead_letter_listing_filter_and_limit(e2e):
    """3.12 Dead-letter listing supports filtering and limits."""
    for idx in range(5):
        agent_id = "agent_A" if idx < 3 else "agent_B"
        await e2e.db.insert_task(agent_id=agent_id, status="dead_letter", dead_letter_reason="non_retryable_error")

    filtered = e2e.api.get_dead_letters(agent_id="agent_A", limit=50)["body"]["items"]
    assert filtered
    assert all(item["agent_id"] == "agent_A" for item in filtered)

    limited = e2e.api.get_dead_letters(limit=2)["body"]["items"]
    assert len(limited) == 2
    assert all("task_id" in item and "dead_letter_reason" in item for item in limited)
