import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolCall


@pytest.mark.asyncio
async def test_3_26_zombie_checkpointer_protection(e2e):
    """3.26 Revoked lease should prevent further checkpoint writes and final completion."""

    async def _slow_final(*args, **kwargs):
        del args, kwargs
        await asyncio.sleep(20)
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
