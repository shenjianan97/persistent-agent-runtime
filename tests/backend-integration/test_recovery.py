import pytest

from helpers.mock_llm import always_fails, retryable_then_success, slow_response


@pytest.mark.asyncio
async def test_3_5_worker_crash_lease_expiry_recovery(e2e):
    """3.5 Simulate expired lease and verify reaper requeues then completion succeeds."""
    e2e.use_llm(slow_response(delay=3.0, content="recovered after reclaim"))
    await e2e.start_worker("e2e-recovery-worker")

    e2e.ensure_agent()
    task_id = e2e.submit_task(input="recover me")

    async def _running() -> bool:
        return e2e.get_task(task_id)["status"] == "running"

    await e2e.wait_for(_running, timeout=10.0, description="task running before simulated crash")

    await e2e.db.update_task(
        task_id,
        status="running",
        lease_owner="crashed-worker",
        lease_expiry_sql="NOW() - INTERVAL '1 second'",
    )

    async def _requeued() -> bool:
        row = await e2e.db.fetch_task_columns(task_id, "status", "retry_count", "lease_owner")
        return bool(row and row["status"] == "queued" and row["retry_count"] >= 1 and row["lease_owner"] is None)

    await e2e.wait_for(_requeued, timeout=15.0, description="reaper requeue after expired lease")
    done = await e2e.wait_for_status(task_id, "completed", timeout=20.0)
    assert done["status"] == "completed"


@pytest.mark.asyncio
async def test_3_6_retryable_error_with_backoff(e2e):
    """3.6 Retryable failure should requeue with retry_after and then succeed."""
    e2e.use_llm(retryable_then_success("503 Service Unavailable", "recovered"))
    await e2e.start_worker("e2e-retry-worker")

    e2e.ensure_agent()
    task_id = e2e.submit_task(max_retries=3, input="transient failure")

    async def _failed_once() -> bool:
        row = await e2e.db.fetch_task_columns(task_id, "status", "retry_count", "retry_after", "last_error_code", "last_error_message")
        return bool(row and row["status"] == "queued" and row["retry_count"] >= 1 and row["retry_after"] is not None)

    await e2e.wait_for(_failed_once, timeout=15.0, description="retryable failure and requeue")

    row = await e2e.db.fetch_task_columns(task_id, "last_error_code", "last_error_message")
    assert row is not None
    assert row["last_error_code"] == "retryable_error"
    assert row["last_error_message"] is not None

    done = await e2e.wait_for_status(task_id, "completed", timeout=25.0)
    assert done["status"] == "completed"


@pytest.mark.asyncio
async def test_3_7_retries_exhausted_dead_letter(e2e):
    """3.7 Retryable failures exceeding max_retries should dead-letter."""
    e2e.use_llm(always_fails("503 Service Unavailable"))
    await e2e.start_worker("e2e-retries-exhausted")

    e2e.ensure_agent()
    task_id = e2e.submit_task(max_retries=1, input="always fail 503")
    dead = await e2e.wait_for_status(task_id, "dead_letter", timeout=25.0)

    assert dead["dead_letter_reason"] == "retries_exhausted"
    assert dead["retry_count"] == 1
