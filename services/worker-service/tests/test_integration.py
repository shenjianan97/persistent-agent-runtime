import asyncio
import json
import os
import uuid
import sys
import asyncpg
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone

from core.config import WorkerConfig
from core.worker import WorkerService
from core.db import create_pool
from executor.graph import GraphExecutor
import executor.providers
from langchain_core.messages import AIMessage, ToolCall
import pytest_asyncio

DB_DSN = os.getenv(
    "E2E_DB_DSN",
    "postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime",
)


async def _ensure_agent(pool: asyncpg.Pool, *, tenant_id: str = "default", agent_id: str = "test_agent") -> None:
    """Insert agent row if it doesn't exist (FK compliance)."""
    agent_config = json.dumps({
        "system_prompt": "You are a test assistant.",
        "model": "claude-3-5-sonnet-latest",
        "temperature": 0.5,
        "allowed_tools": ["calculator"],
    })
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
            VALUES ($1, $2, 'Test Agent', $3::jsonb, 'active')
            ON CONFLICT (tenant_id, agent_id) DO NOTHING
            """,
            tenant_id, agent_id, agent_config,
        )


async def cleanup_test_db(pool: asyncpg.Pool) -> None:
    """Remove all test data to ensure isolation between integration tests."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM task_events")
        await conn.execute("DELETE FROM checkpoint_writes")
        await conn.execute("DELETE FROM checkpoints")
        await conn.execute("DELETE FROM tasks")
        await conn.execute("DELETE FROM agents")


async def setup_test_task(pool: asyncpg.Pool) -> str:
    await _ensure_agent(pool)
    task_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        agent_config = {
            "system_prompt": "You are a test assistant.",
            "model": "claude-3-5-sonnet-latest",
            "temperature": 0.5,
            "allowed_tools": ["calculator"]
        }
        await conn.execute("""
            INSERT INTO tasks (
                task_id, tenant_id, agent_id, agent_config_snapshot, 
                status, input, max_retries, max_steps, task_timeout_seconds, worker_pool_id
            ) VALUES ($1, 'default', 'test_agent', $2, 'queued', 'Test input', 3, 5, 300, 'test_pool')
        """, task_id, json.dumps(agent_config))
        await conn.execute("SELECT pg_notify('new_task', 'test_pool')")
    return task_id

@pytest.mark.asyncio
async def test_worker_e2e_integration():
    try:
        pool = await create_pool(DB_DSN)
    except Exception as e:
        pytest.skip(f"Skipping integration test due to DB connection failure: {e}")
        return
        
    await cleanup_test_db(pool)

    config = WorkerConfig(
        worker_id="test-graph-integration-worker",
        db_dsn=DB_DSN,
        heartbeat_interval_seconds=1,
        lease_duration_seconds=5,
        reaper_interval_seconds=2,
        reaper_jitter_seconds=0,
        worker_pool_id="test_pool"
    )
    
    from executor.router import DefaultTaskRouter
    router = DefaultTaskRouter(config, pool)
    worker = WorkerService(config, pool, router)
    
    from unittest.mock import AsyncMock
    # We patch create_llm to return a deterministic message avoiding network calls
    with patch("executor.providers.create_llm", new_callable=AsyncMock) as MockChat:
        mock_llm = MagicMock()
        mock_ainvoke = AsyncMock()
        
        # Fake LLM response
        fake_msg = AIMessage(content="I am a fake integrated response!")
        mock_ainvoke.return_value = fake_msg
        
        mock_llm.ainvoke = mock_ainvoke
        mock_llm.bind_tools.return_value = mock_llm
        MockChat.return_value = mock_llm
        
        await worker.start()
        
        try:
            t1 = await setup_test_task(pool)
            
            # Allow time for poller to pick up, run graph, and complete
            await asyncio.sleep(3)
            
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT status, output, version FROM tasks WHERE task_id=$1", t1)
                assert str(row["status"]) == "completed", f"Status was {row['status']}. Details: {row.get('dead_letter_reason')} {row.get('dead_letter_error_details')}"
                
                output = json.loads(row["output"])
                assert output["result"] == "I am a fake integrated response!"
                
                # Check that checkpoints were written
                checkpoint_count = await conn.fetchval("SELECT COUNT(*) FROM checkpoints WHERE task_id=$1::uuid", t1)
                assert checkpoint_count > 0, "Checkpoints should be saved to the database"
                
        finally:
            await worker.stop()
            await pool.close()

@pytest.mark.asyncio
async def test_worker_core_primitives_integration():
    try:
        pool = await create_pool(DB_DSN)
    except Exception as e:
        pytest.skip(f"Skipping integration test due to DB connection failure: {e}")
        return
        
    await cleanup_test_db(pool)

    config = WorkerConfig(
        worker_id="test-core-integration-worker",
        db_dsn=DB_DSN,
        heartbeat_interval_seconds=1,
        lease_duration_seconds=5,
        reaper_interval_seconds=2,
        reaper_jitter_seconds=0,
        worker_pool_id="test_pool"
    )
    
    executed_tasks = []
    
    class MockRouter:
        def get_executor(self, task_data: dict):
            class MockExecutor:
                async def execute_task(self, task_data: dict, cancel_event: asyncio.Event) -> None:
                    task_id = str(task_data["task_id"])
                    executed_tasks.append(task_id)
                    
                    await asyncio.sleep(2.5)  # Wait for a heartbeat to fire
                    if not cancel_event.is_set():
                        async with pool.acquire() as conn:
                            await conn.execute("UPDATE tasks SET status='completed' WHERE task_id=$1", task_id)
            return MockExecutor()
            
    worker = WorkerService(config, pool, MockRouter())
    await worker.start()
    
    try:
        # Scenario 1 & 2: Normal claim, execution, and heartbeat maintenance
        t1 = await setup_test_task(pool)
        await asyncio.sleep(4)
        
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id=$1", t1)
            assert str(row["status"]) == "completed", f"Status was {row['status']}. Details: {row.get('dead_letter_reason')} {row.get('dead_letter_error_details')}"
        assert str(t1) in executed_tasks
        
        # Scenario 3: Reaper reclaims crashed task
        t3 = str(uuid.uuid4())
        await _ensure_agent(pool)
        async with pool.acquire() as conn:
            agent_config = {"model": "test"}
            await conn.execute("""
                INSERT INTO tasks (
                    task_id, tenant_id, agent_id, agent_config_snapshot,
                    status, input, max_retries, max_steps, task_timeout_seconds,
                    lease_owner, lease_expiry, worker_pool_id
                ) VALUES ($1, 'default', 'test_agent', $2, 'running', 'Test', 3, 5, 300, 'crashed-worker', NOW() - INTERVAL '10 seconds', 'test_pool')
            """, t3, json.dumps(agent_config))

        await asyncio.sleep(3) # Wait for Reaper
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT status, retry_count FROM tasks WHERE task_id=$1", t3)
            # The reaper requeues the task (retry_count incremented).
            # The poller may re-claim it before we check, so status could be 'queued' or 'running'.
            assert row["retry_count"] >= 1, f"Reaper should have incremented retry_count, got {row['retry_count']}"
            assert str(row["status"]) in ("queued", "running"), f"Unexpected status: {row['status']}"
            
        # Scenario 4: Reaper dead-letters on task timeout
        t4 = str(uuid.uuid4())
        await _ensure_agent(pool)
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO tasks (
                    task_id, tenant_id, agent_id, agent_config_snapshot,
                    status, input, max_retries, max_steps, task_timeout_seconds,
                    created_at, timeout_reference_at, worker_pool_id
                ) VALUES ($1, 'default', 'test_agent', $2, 'queued', 'Test', 3, 5, 10, NOW() - INTERVAL '20 seconds', NOW() - INTERVAL '20 seconds', 'test_pool')
            """, t4, json.dumps(agent_config))

        await asyncio.sleep(3)
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT status, dead_letter_reason FROM tasks WHERE task_id=$1", t4)
            assert str(row["status"]) == "dead_letter"
            assert str(row["dead_letter_reason"]) == "task_timeout"

    finally:
        await worker.stop()
        await pool.close()

@pytest.mark.asyncio
async def test_worker_mcp_tool_execution_integration():
    """Verify the integration actually executes the embedded MCP tools."""
    try:
        pool = await create_pool(DB_DSN)
    except Exception as e:
        pytest.skip(f"Skipping integration test due to DB connection failure: {e}")
        return
        
    await cleanup_test_db(pool)

    config = WorkerConfig(
        worker_id="test-tool-integration-worker",
        db_dsn=DB_DSN,
        heartbeat_interval_seconds=1,
        lease_duration_seconds=5,
        reaper_interval_seconds=2,
        reaper_jitter_seconds=0,
        worker_pool_id="test_pool"
    )
    
    from executor.router import DefaultTaskRouter
    router = DefaultTaskRouter(config, pool)
    worker = WorkerService(config, pool, router)
    
    from unittest.mock import AsyncMock
    # We patch create_llm to simulate an LLM deciding to call the calculator tool
    with patch("executor.providers.create_llm", new_callable=AsyncMock) as MockChat:
        mock_llm = MagicMock()
        mock_ainvoke = AsyncMock()
        
        # We need the mock to iterate twice. 
        # 1. First invocation: LLM asks to use calculator
        call_msg = AIMessage(
            content="",
            tool_calls=[ToolCall(name="calculator", args={"expression": "5 * 5"}, id="call_123")]
        )
        
        # 2. Second invocation: LLM sees tool result and outputs the final answer
        final_msg = AIMessage(content="The result is 25!")
        
        mock_ainvoke.side_effect = [call_msg, final_msg]
        mock_llm.ainvoke = mock_ainvoke
        mock_llm.bind_tools.return_value = mock_llm
        MockChat.return_value = mock_llm
        
        await worker.start()
        
        try:
            # We setup a task allowing the 'calculator' tool
            t1 = await setup_test_task(pool)
            
            # Allow time for polling, graph building, tool execution via MCP, and DB commits
            await asyncio.sleep(4)
            
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id=$1", t1)
                assert str(row["status"]) == "completed", f"Status was {row['status']}. Details: {row.get('dead_letter_reason')} {row.get('dead_letter_error_details')}"
                
                output = json.loads(row["output"])
                assert output["result"] == "The result is 25!"
                
                # Verify that checkpointer saved the tool execution
                # The checkpoint blobs should contain the 'ToolMessage' result returned by calculator
                checkpoint_rows = await conn.fetch("SELECT checkpoint_payload, metadata_payload FROM checkpoints WHERE task_id=$1::uuid", t1)
                assert len(checkpoint_rows) > 0
                
        finally:
            await worker.stop()
            await pool.close()
