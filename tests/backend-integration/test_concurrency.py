import concurrent.futures
import time

import pytest

from helpers.mock_llm import simple_response, slow_response


@pytest.mark.asyncio
async def test_3_16_concurrent_task_execution(e2e):
    """3.16 Submit multiple tasks and verify all complete once."""
    e2e.use_llm_factory(lambda: simple_response("ok"))
    await e2e.start_worker("e2e-concurrent", config_overrides={"max_concurrent_tasks": 5})

    e2e.ensure_agent()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(e2e.submit_task, input=f"task-{i}") for i in range(5)]
        task_ids = [f.result() for f in futures]

    for task_id in task_ids:
        done = await e2e.wait_for_status(task_id, "completed", timeout=25.0)
        assert done["status"] == "completed"

    for task_id in task_ids:
        checkpoints = await e2e.db.fetch_checkpoints(task_id)
        checkpoint_ids = [row["checkpoint_id"] for row in checkpoints]
        assert len(checkpoint_ids) == len(set(checkpoint_ids))


@pytest.mark.asyncio
async def test_3_17_multi_worker_coordination(e2e):
    """3.17 Two workers should split work without double-claiming."""
    e2e.use_llm_factory(lambda: simple_response("multi-worker"))
    await e2e.start_worker("e2e-worker-a")
    await e2e.start_worker("e2e-worker-b")

    e2e.ensure_agent()
    task_ids = [e2e.submit_task(input=f"mw-{i}") for i in range(6)]
    for task_id in task_ids:
        done = await e2e.wait_for_status(task_id, "completed", timeout=30.0)
        assert done["status"] == "completed"

    workers_seen: set[str] = set()
    for task_id in task_ids:
        checkpoints = await e2e.db.fetch_checkpoints(task_id)
        per_task_workers = {cp["worker_id"] for cp in checkpoints if cp["worker_id"]}
        assert per_task_workers
        workers_seen.update(per_task_workers)

    assert len(workers_seen) >= 2


@pytest.mark.asyncio
async def test_3_18_listen_notify_fast_path(e2e):
    """3.18 LISTEN/NOTIFY should wake an idle worker quickly."""
    e2e.use_llm(slow_response(delay=2.0, content="slow"))
    await e2e.start_worker("e2e-notify")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": []
    })
    start = time.monotonic()
    task_id = e2e.submit_task(input="latency")

    async def _running() -> bool:
        return e2e.get_task(task_id)["status"] == "running"

    await e2e.wait_for(_running, timeout=5.0, interval=0.05, description="task claimed quickly")
    assert time.monotonic() - start < 1.5

    await e2e.wait_for_status(task_id, "completed", timeout=20.0)
