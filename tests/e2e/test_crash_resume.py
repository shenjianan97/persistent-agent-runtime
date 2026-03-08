from unittest.mock import patch

import pytest

from helpers.mock_llm import simple_response, tool_then_retryable_then_success


@pytest.mark.asyncio
async def test_3_19_crash_recovery_node_resume_boundary(e2e):
    """3.19 Retry after tool step should append checkpoints and preserve prior ones."""
    e2e.use_llm(tool_then_retryable_then_success(expression="5*5", final_answer="done"))
    await e2e.start_worker("e2e-crash-a")

    task_id = e2e.submit_task(max_retries=3, allowed_tools=["calculator"], input="resume test")

    async def _requeued() -> bool:
        row = await e2e.db.fetch_task_columns(task_id, "status", "retry_count")
        return bool(row and row["status"] == "queued" and row["retry_count"] >= 1)

    await e2e.wait_for(_requeued, timeout=20.0, description="task requeued after retryable failure")
    checkpoints_before = await e2e.db.fetch_checkpoints(task_id)
    before_ids = {cp["checkpoint_id"] for cp in checkpoints_before}

    await e2e.stop_workers()
    e2e.use_llm(simple_response("done"))
    await e2e.start_worker("e2e-crash-b")

    done = await e2e.wait_for_status(task_id, "completed", timeout=30.0)
    assert done["status"] == "completed"

    checkpoints_after = await e2e.db.fetch_checkpoints(task_id)
    after_ids = {cp["checkpoint_id"] for cp in checkpoints_after}

    assert len(checkpoints_after) >= len(checkpoints_before)
    assert before_ids.issubset(after_ids)
    assert len(after_ids) == len(checkpoints_after)

    worker_ids = {cp["worker_id"] for cp in checkpoints_after if cp["worker_id"]}
    assert len(worker_ids) >= 2


@pytest.mark.asyncio
async def test_3_20_crash_between_last_checkpoint_and_completion(e2e):
    """3.20 Simulate completion-update loss and verify zero-step resume to completed."""
    e2e.use_llm(simple_response("done"))

    pool = e2e.db.pool
    original_execute = pool.__class__.execute
    state = {"swallowed_once": False}

    async def flaky_execute(self, query, *args, **kwargs):
        if (not state["swallowed_once"]) and "SET status='completed'" in query:
            state["swallowed_once"] = True
            return "UPDATE 0"
        return await original_execute(self, query, *args, **kwargs)

    with patch.object(pool.__class__, "execute", new=flaky_execute):
        await e2e.start_worker("e2e-post-checkpoint-crash")
        task_id = e2e.submit_task(allowed_tools=[], input="single step")

        async def _swallowed() -> bool:
            return state["swallowed_once"]

        await e2e.wait_for(_swallowed, timeout=20.0, description="completion update swallowed once")

        checkpoints_before = await e2e.db.checkpoint_count(task_id)
        await e2e.db.execute(
            "UPDATE tasks SET lease_expiry = NOW() - INTERVAL '1 second', updated_at = NOW() WHERE task_id = $1::uuid",
            task_id,
        )

        async def _terminal() -> bool:
            return e2e.get_task(task_id)["status"] in {"completed", "dead_letter"}

        await e2e.wait_for(_terminal, timeout=25.0, description="task recovered to terminal state")
        final = e2e.get_task(task_id)
        assert final["status"] == "completed"

        checkpoints_after = await e2e.db.checkpoint_count(task_id)
        assert checkpoints_after == checkpoints_before
