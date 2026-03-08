import asyncio

import pytest

from helpers.mock_llm import slow_response


@pytest.mark.asyncio
async def test_3_3_cancel_while_queued(e2e):
    """3.3 Cancel a task before any worker claims it."""
    task_id = e2e.submit_task(input="Stay queued")

    cancel = e2e.api.cancel_task(task_id)["body"]
    assert cancel["status"] == "dead_letter"
    assert cancel["dead_letter_reason"] == "cancelled_by_user"

    task = e2e.get_task(task_id)
    assert task["status"] == "dead_letter"
    assert task["dead_letter_reason"] == "cancelled_by_user"

    row = await e2e.db.fetch_task_columns(task_id, "lease_owner")
    assert row is not None
    assert row["lease_owner"] is None


@pytest.mark.asyncio
async def test_3_4_cancel_while_running(e2e):
    """3.4 Cancel an actively running task."""
    e2e.use_llm(slow_response(delay=30.0, content="slow"))
    await e2e.start_worker("e2e-cancel-running")

    task_id = e2e.submit_task(input="Long task")

    async def _running() -> bool:
        return e2e.get_task(task_id)["status"] == "running"

    await e2e.wait_for(_running, timeout=10.0, description="task running")

    before_count = await e2e.db.checkpoint_count(task_id)
    cancel = e2e.api.cancel_task(task_id)["body"]
    assert cancel["status"] == "dead_letter"
    assert cancel["dead_letter_reason"] == "cancelled_by_user"

    dead = await e2e.wait_for_status(task_id, "dead_letter", timeout=10.0)
    assert dead["dead_letter_reason"] == "cancelled_by_user"

    await asyncio.sleep(2.5)
    assert await e2e.db.checkpoint_count(task_id) == before_count

    row = await e2e.db.fetch_task_columns(task_id, "lease_owner", "lease_expiry", "status", "dead_letter_reason")
    assert row is not None
    assert row["lease_owner"] is None
    assert row["status"] == "dead_letter"
    assert row["dead_letter_reason"] == "cancelled_by_user"
