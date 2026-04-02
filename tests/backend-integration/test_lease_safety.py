import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from langchain_core.messages import AIMessage, ToolCall


DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime",
)


@pytest.mark.asyncio
async def test_3_26_zombie_checkpointer_protection(e2e):
    """3.26 Revoked lease should prevent further checkpoint writes and final completion."""

    async def _slow_final(*args, **kwargs):
        del args, kwargs
        await asyncio.sleep(5)
        return AIMessage(content="final")

    first_turn = AIMessage(
        content="",
        tool_calls=[ToolCall(name="calculator", args={"expression": "5*5"}, id="call_lease")],
    )
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(side_effect=[first_turn, _slow_final])

    e2e.use_llm(mock_llm)
    await e2e.start_worker("e2e-lease-safety")

    e2e.ensure_agent()
    task_id = e2e.submit_task(input="lease revoke")

    async def _first_checkpoint_written() -> bool:
        return (await e2e.db.checkpoint_count(task_id)) >= 1

    await e2e.wait_for(_first_checkpoint_written, timeout=15.0, description="first checkpoint written")
    before = await e2e.db.checkpoint_count(task_id)

    # Revoke the lease out-of-band to simulate split-brain protection.
    await e2e.db.execute(
        """
        UPDATE tasks
        SET lease_owner=NULL,
            lease_expiry=NULL,
            status='dead_letter',
            dead_letter_reason='cancelled_by_user',
            dead_lettered_at=NOW(),
            updated_at=NOW()
        WHERE task_id=$1::uuid
        """,
        task_id,
    )

    dead = await e2e.wait_for_status(task_id, "dead_letter", timeout=10.0)
    assert dead["status"] == "dead_letter"

    await asyncio.sleep(2.5)
    after = await e2e.db.checkpoint_count(task_id)
    assert after == before

    final = e2e.get_task(task_id)
    assert final["status"] == "dead_letter"


@pytest.mark.asyncio
async def test_3_26_worker_stop_releases_db_cleanup_after_zombie_revocation(e2e):
    """Worker stop should not leave a DB-blocking zombie execution behind."""

    async def _very_slow_final(*args, **kwargs):
        del args, kwargs
        await asyncio.sleep(30)
        return AIMessage(content="final")

    first_turn = AIMessage(
        content="",
        tool_calls=[ToolCall(name="calculator", args={"expression": "6*7"}, id="call_cleanup")],
    )
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(side_effect=[first_turn, _very_slow_final])

    e2e.use_llm(mock_llm)
    worker = await e2e.start_worker(
        "e2e-lease-cleanup",
        config_overrides={"shutdown_drain_seconds": 1},
    )

    e2e.ensure_agent()
    task_id = e2e.submit_task(input="lease revoke cleanup")

    async def _first_checkpoint_written() -> bool:
        return (await e2e.db.checkpoint_count(task_id)) >= 1

    await e2e.wait_for(_first_checkpoint_written, timeout=15.0, description="first checkpoint written")

    await e2e.db.execute(
        """
        UPDATE tasks
        SET lease_owner=NULL,
            lease_expiry=NULL,
            status='dead_letter',
            dead_letter_reason='cancelled_by_user',
            dead_lettered_at=NOW(),
            updated_at=NOW()
        WHERE task_id=$1::uuid
        """,
        task_id,
    )

    dead = await e2e.wait_for_status(task_id, "dead_letter", timeout=10.0)
    assert dead["status"] == "dead_letter"

    await e2e.stop_worker(worker)

    conn = await asyncpg.connect(DB_DSN)
    try:
        await asyncio.wait_for(
            conn.execute("DELETE FROM task_events WHERE task_id = $1::uuid", task_id),
            timeout=1.0,
        )
        await asyncio.wait_for(
            conn.execute("DELETE FROM tasks WHERE task_id = $1::uuid", task_id),
            timeout=1.0,
        )
    finally:
        await conn.close()
