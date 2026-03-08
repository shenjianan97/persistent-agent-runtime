import asyncio

import pytest

from helpers.mock_llm import simple_response


@pytest.mark.asyncio
async def test_3_21_reaper_expired_lease_retries_exhausted_dead_letter(e2e):
    """3.21 Reaper should dead-letter expired lease when retries are already exhausted."""
    e2e.use_llm(simple_response("unused"))
    await e2e.start_worker(
        "e2e-reaper-expired",
        config_overrides={"max_concurrent_tasks": 0, "reaper_interval_seconds": 2, "reaper_jitter_seconds": 0},
    )

    task_id = await e2e.db.insert_task(
        status="running",
        max_retries=0,
        retry_count=0,
        lease_owner="crashed-worker",
        lease_expiry_sql="NOW() - INTERVAL '1 minute'",
    )

    async def _dead_lettered() -> bool:
        row = await e2e.db.fetch_task_columns(task_id, "status", "dead_letter_reason", "last_worker_id", "last_error_code")
        return bool(
            row
            and row["status"] == "dead_letter"
            and row["dead_letter_reason"] == "retries_exhausted"
            and row["last_worker_id"] == "crashed-worker"
            and row["last_error_code"] == "retries_exhausted"
        )

    await e2e.wait_for(_dead_lettered, timeout=12.0, description="reaper dead-letter exhausted expired lease")


@pytest.mark.asyncio
async def test_3_22_retry_backoff_invisibility_window(e2e):
    """3.22 retry_after should hide queued tasks from claimers until window expires."""
    task_id = e2e.submit_task(input="retry window")
    await e2e.db.set_retry_after_future(task_id, 30)

    e2e.use_llm(simple_response("claimed after retry_after"))
    await e2e.start_worker("e2e-backoff-window")

    await asyncio.sleep(3.0)
    row = await e2e.db.fetch_task_columns(task_id, "status", "lease_owner")
    assert row is not None
    assert row["status"] == "queued"
    assert row["lease_owner"] is None

    await e2e.db.set_retry_after_past(task_id)
    await e2e.db.notify_new_task()

    async def _claimed_or_done() -> bool:
        return e2e.get_task(task_id)["status"] in {"running", "completed"}

    await e2e.wait_for(_claimed_or_done, timeout=10.0, description="task claim after retry_after expires")


@pytest.mark.asyncio
async def test_3_28_multi_reaper_coordination(e2e):
    """3.28 Two reapers should reclaim each expired lease exactly once."""
    e2e.use_llm(simple_response("unused"))
    overrides = {"max_concurrent_tasks": 0, "reaper_interval_seconds": 2, "reaper_jitter_seconds": 0}
    await e2e.start_worker("e2e-reaper-a", config_overrides=overrides)
    await e2e.start_worker("e2e-reaper-b", config_overrides=overrides)

    task_ids: list[str] = []
    for _ in range(5):
        task_id = await e2e.db.insert_task(
            status="running",
            max_retries=3,
            retry_count=0,
            lease_owner="crashed-worker",
            lease_expiry_sql="NOW() - INTERVAL '1 minute'",
        )
        task_ids.append(task_id)

    async def _all_reclaimed_once() -> bool:
        for task_id in task_ids:
            row = await e2e.db.fetch_task_columns(task_id, "status", "retry_count")
            if not row or row["status"] != "queued" or row["retry_count"] != 1:
                return False
        return True

    await e2e.wait_for(_all_reclaimed_once, timeout=15.0, description="all expired leases reclaimed once")
