import pytest

from helpers.mock_llm import dev_sleep_tool_call, simple_response, slow_response


@pytest.mark.asyncio
async def test_dev_expire_lease_recovers_with_preserved_checkpoints(e2e):
    """Dev task-control lease expiry should trigger recovery without losing prior checkpoints."""
    e2e.use_llm(slow_response(delay=8.0, content="recovered after dev lease expiry"))
    worker_a = await e2e.start_worker("e2e-dev-crash-a")

    task_id = e2e.submit_task(allowed_tools=[], input="recover through dev task controls")

    async def _checkpointed() -> bool:
        row = await e2e.db.fetch_task_columns(task_id, "status")
        return bool(row and row["status"] == "running" and await e2e.db.checkpoint_count(task_id) >= 2)

    await e2e.wait_for(_checkpointed, timeout=10.0, description="task running with initial checkpoints")
    checkpoints_before = e2e.get_checkpoints(task_id)
    before_ids = {cp["checkpoint_id"] for cp in checkpoints_before}
    assert before_ids

    await e2e.start_worker("e2e-dev-crash-b")
    forced = e2e.dev_expire_lease(task_id, lease_owner="crashed-worker")
    assert forced["status"] == "running"
    await e2e.stop_worker(worker_a)

    done = await e2e.wait_for_status(task_id, "completed", timeout=25.0)
    assert done["status"] == "completed"
    assert done["retry_count"] >= 1

    checkpoints_after = e2e.get_checkpoints(task_id)
    after_ids = {cp["checkpoint_id"] for cp in checkpoints_after}
    assert before_ids.issubset(after_ids)
    assert len(checkpoints_after) > len(checkpoints_before)

    worker_ids = {cp["worker_id"] for cp in checkpoints_after if cp["worker_id"]}
    assert "e2e-dev-crash-a" in worker_ids
    assert "e2e-dev-crash-b" in worker_ids


@pytest.mark.asyncio
async def test_dev_force_dead_letter_redrive_preserves_checkpoints(e2e):
    """Dev task-control dead-letter should support redrive resume without discarding checkpoints."""
    e2e.use_llm(slow_response(delay=8.0, content="should not complete before redrive"))
    await e2e.start_worker("e2e-dev-redrive")

    task_id = e2e.submit_task(allowed_tools=[], input="force dead letter after checkpoints")

    async def _checkpointed() -> bool:
        row = await e2e.db.fetch_task_columns(task_id, "status")
        return bool(row and row["status"] == "running" and await e2e.db.checkpoint_count(task_id) >= 2)

    await e2e.wait_for(_checkpointed, timeout=10.0, description="task running with initial checkpoints")
    checkpoints_before = e2e.get_checkpoints(task_id)
    before_ids = {cp["checkpoint_id"] for cp in checkpoints_before}
    assert before_ids

    dead = e2e.dev_force_dead_letter(
        task_id,
        reason="non_retryable_error",
        error_message="Forced dead letter for redrive resume test",
    )
    assert dead["status"] == "dead_letter"

    dead_task = await e2e.wait_for_status(task_id, "dead_letter", timeout=10.0)
    assert dead_task["dead_letter_reason"] == "non_retryable_error"
    assert dead_task["last_error_message"] == "Forced dead letter for redrive resume test"

    e2e.use_llm(simple_response("success after dev redrive"))
    redrive = e2e.api.redrive_task(task_id)["body"]
    assert redrive["status"] == "queued"

    done = await e2e.wait_for_status(task_id, "completed", timeout=25.0)
    assert done["status"] == "completed"
    assert done["dead_letter_reason"] is None

    checkpoints_after = e2e.get_checkpoints(task_id)
    after_ids = {cp["checkpoint_id"] for cp in checkpoints_after}
    assert before_ids.issubset(after_ids)
    assert len(checkpoints_after) > len(checkpoints_before)


@pytest.mark.asyncio
async def test_dev_sleep_tool_supports_short_timeout_testing(e2e):
    """Dev task-control sleep tool should make timeout testing deterministic without DB edits."""
    e2e.use_llm(dev_sleep_tool_call(seconds=3, final_answer="this should never be reached"))
    await e2e.start_worker("e2e-dev-sleep-timeout")

    task_id = e2e.submit_task(
        allowed_tools=["dev_sleep"],
        task_timeout_seconds=1,
        input="Call dev_sleep for 3 seconds, then answer.",
    )

    dead = await e2e.wait_for_status(task_id, "dead_letter", timeout=10.0)
    assert dead["status"] == "dead_letter"
    assert dead["dead_letter_reason"] == "task_timeout"

    checkpoints = e2e.get_checkpoints(task_id)
    tool_calls = [cp for cp in checkpoints if cp["event"] and cp["event"]["type"] == "tool_call"]
    assert tool_calls
    assert tool_calls[0]["event"]["tool_name"] == "dev_sleep"


@pytest.mark.asyncio
async def test_worker_heartbeat_restores_registry_and_health_after_offline_mark(e2e):
    """A live worker should restore its own registry row and health count on heartbeat."""
    worker_id = "e2e-worker-registry-recovery"
    await e2e.start_worker(
        worker_id,
        config_overrides={
            "heartbeat_interval_seconds": 1,
        },
    )

    await e2e.db.execute(
        "UPDATE workers SET status = 'offline', last_heartbeat_at = NOW() - INTERVAL '5 minutes' WHERE worker_id = $1",
        worker_id,
    )

    async def _health_recovered() -> bool:
        health = e2e.api.health()["body"]
        if health["active_workers"] < 1:
            return False
        status = await e2e.db.fetchval("SELECT status FROM workers WHERE worker_id = $1", worker_id)
        return status == "online"

    await e2e.wait_for(_health_recovered, timeout=10.0, description="worker registry recovers to online")
