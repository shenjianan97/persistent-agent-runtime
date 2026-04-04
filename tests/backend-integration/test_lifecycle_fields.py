import pytest

from helpers.mock_llm import always_fails, retryable_then_success, simple_response


@pytest.mark.asyncio
async def test_3_23_version_field_increments_on_transitions(e2e):
    """3.23 version should increment across claim/completion/cancel/redrive transitions."""
    e2e.use_llm(simple_response("done"))
    await e2e.start_worker("e2e-version")

    e2e.ensure_agent()
    task_id = e2e.submit_task(input="version path")
    created = await e2e.db.fetch_task_columns(task_id, "version")
    assert created is not None and created["version"] == 1

    async def _claimed() -> bool:
        row = await e2e.db.fetch_task_columns(task_id, "version")
        return bool(row and row["version"] >= 2)

    await e2e.wait_for(_claimed, timeout=10.0, description="version increment on claim")

    done = await e2e.wait_for_status(task_id, "completed", timeout=20.0)
    assert done["status"] == "completed"
    after = await e2e.db.fetch_task_columns(task_id, "version")
    assert after is not None and after["version"] >= 3

    await e2e.stop_workers()

    second_task = e2e.submit_task(input="cancel redrive")
    before_cancel = await e2e.db.fetch_task_columns(second_task, "version")

    cancel_resp = e2e.api.cancel_task(second_task)["body"]
    assert cancel_resp["status"] == "dead_letter"

    after_cancel = await e2e.db.fetch_task_columns(second_task, "version")
    assert before_cancel is not None and after_cancel is not None
    assert after_cancel["version"] > before_cancel["version"]

    redrive = e2e.api.redrive_task(second_task)["body"]
    assert redrive["status"] == "queued"

    after_redrive = await e2e.db.fetch_task_columns(second_task, "version")
    assert after_redrive is not None
    assert after_redrive["version"] > after_cancel["version"]


@pytest.mark.asyncio
async def test_3_24_error_fields_cleared_on_completion(e2e):
    """3.24 last_error fields should be cleared after successful retry completion."""
    e2e.use_llm(retryable_then_success("503 Service Unavailable", "ok"))
    await e2e.start_worker("e2e-error-clear")

    e2e.ensure_agent()
    task_id = e2e.submit_task(max_retries=3, input="clear errors")

    async def _error_set() -> bool:
        row = await e2e.db.fetch_task_columns(task_id, "status", "last_error_code", "last_error_message")
        return bool(row and row["status"] == "queued" and row["last_error_code"] == "retryable_error")

    await e2e.wait_for(_error_set, timeout=15.0, description="error fields populated after first failure")
    await e2e.wait_for_status(task_id, "completed", timeout=25.0)

    final = await e2e.db.fetch_task_columns(task_id, "last_error_code", "last_error_message")
    assert final is not None
    assert final["last_error_code"] is None
    assert final["last_error_message"] is None


@pytest.mark.asyncio
async def test_3_25_retry_history_append_only(e2e):
    """3.25 retry_history should contain one timestamp per retry attempt."""
    e2e.use_llm(always_fails("503 Service Unavailable"))
    await e2e.start_worker("e2e-retry-history")

    e2e.ensure_agent()
    task_id = e2e.submit_task(max_retries=2, input="retry history")
    dead = await e2e.wait_for_status(task_id, "dead_letter", timeout=30.0)
    assert dead["dead_letter_reason"] == "retries_exhausted"

    row = await e2e.db.fetch_task_columns(task_id, "retry_history")
    assert row is not None

    history = e2e.parse_json_array(row.get("retry_history"))
    assert len(history) == 2
    assert history == sorted(history)
