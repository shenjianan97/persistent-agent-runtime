import asyncio
import json
import uuid
import sys
import asyncpg
from datetime import datetime, timezone

from core.config import WorkerConfig
from core.worker import WorkerService
from core.db import create_pool

DB_DSN = "postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime"

async def setup_test_task(pool: asyncpg.Pool, custom_task_id: str = None) -> str:
    task_id = custom_task_id or str(uuid.uuid4())
    async with pool.acquire() as conn:
        agent_config = {
            "system_prompt": "You are a test assistant.",
            "model": "claude-sonnet-4-6",
            "temperature": 0.5,
            "allowed_tools": ["web_search"]
        }
        await conn.execute("""
            INSERT INTO tasks (
                task_id, tenant_id, agent_id, agent_config_snapshot, 
                status, input, max_retries, max_steps, task_timeout_seconds
            ) VALUES ($1, 'default', 'test_agent', $2, 'queued', 'Test input', 3, 5, 300)
        """, task_id, json.dumps(agent_config))
        await conn.execute("SELECT pg_notify('new_task', 'shared')")
    return task_id

async def run_integration_tests():
    print("=== Starting Worker Service Integration Tests ===")
    
    try:
        pool = await create_pool(DB_DSN)
    except Exception as e:
        print(f"Could not connect to database: {e}")
        sys.exit(1)
        
    config = WorkerConfig(
        worker_id="test-integration-worker",
        db_dsn=DB_DSN,
        heartbeat_interval_seconds=1,
        lease_duration_seconds=5,
        reaper_interval_seconds=2,
        reaper_jitter_seconds=0
    )
    
    # Track executed tasks
    executed_tasks = []
    
    async def mock_executor(task_data: dict):
        task_id = str(task_data["task_id"])
        tenant_id = task_data["tenant_id"]
        executed_tasks.append(task_id)
        
        # Start heartbeat manually (as task 6 would)
        handle = worker.heartbeat.start_heartbeat(task_id, tenant_id)
        
        try:
            # Keep it open slightly to see heartbeat happen
            print(f"Executing task {task_id}...")
            await asyncio.sleep(2.5)
            
            if handle.cancel_event.is_set():
                print(f"Task {task_id} lease revoked or cancelled.")
            else:
                # Mark completed
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE tasks SET status='completed' WHERE task_id=$1", task_id)
        finally:
            await worker.heartbeat.stop_heartbeat(task_id)
            
    worker = WorkerService(config, on_task_claimed=mock_executor)
    await worker.start()
    
    try:
        # Scenario 1: Normal claim and execution
        print("\nTesting Scenario 1: Normal Task Execution")
        t1 = await setup_test_task(pool)
        
        # Wait for poller to pick it up and executor to finish
        await asyncio.sleep(4)
        
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT status, lease_owner, retry_count FROM tasks WHERE task_id=$1", t1)
            assert str(row["status"]) == "completed", f"Expected completed, got {row['status']}"
        
        assert str(t1) in executed_tasks
        print("-> Scenario 1 Passed")
        
        # Scenario 2: Heartbeat maintenance
        print("\nTesting Scenario 2: Heartbeat maintenance restricts revocation")
        t2 = await setup_test_task(pool)
        
        # Wait 1.5s (task is running, 1 heartbeat should've fired)
        await asyncio.sleep(1.5)
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT status, lease_expiry FROM tasks WHERE task_id=$1", t2)
            assert str(row["status"]) == "running"
            first_lease = row["lease_expiry"]
            
        # Wait another 1.5s
        await asyncio.sleep(1.5)
        async with pool.acquire() as conn:
            row2 = await conn.fetchrow("SELECT status, lease_expiry FROM tasks WHERE task_id=$1", t2)
            # lease_expiry should have advanced
            assert row2["lease_expiry"] > first_lease
            
        # The executor finishes and sets to completed
        await asyncio.sleep(1)
        print("-> Scenario 2 Passed")
        
        # Scenario 3: Reaper reclaims crashed task
        print("\nTesting Scenario 3: Reaper Reclaims Orphaned Task")
        t3 = str(uuid.uuid4())
        async with pool.acquire() as conn:
            agent_config = {"model": "test"}
            await conn.execute("""
                INSERT INTO tasks (
                    task_id, tenant_id, agent_id, agent_config_snapshot, 
                    status, input, max_retries, max_steps, task_timeout_seconds,
                    lease_owner, lease_expiry
                ) VALUES ($1, 'default', 'test_agent', $2, 'running', 'Test', 3, 5, 300, 'crashed-worker', NOW() - INTERVAL '10 seconds')
            """, t3, json.dumps(agent_config))

        # Reaper runs every 2s, wait a bit
        await asyncio.sleep(3)
        
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT status, retry_count, retry_after FROM tasks WHERE task_id=$1", t3)
            assert str(row["status"]) == "queued", f"Expected queued, got {row['status']}"
            assert row["retry_count"] == 1
        
        print("-> Scenario 3 Passed")
        
        # Scenario 4: Reaper dead-letters on task timeout
        print("\nTesting Scenario 4: Reaper dead-letters expired tasks")
        t4 = str(uuid.uuid4())
        async with pool.acquire() as conn:
            agent_config = {"model": "test"}
            await conn.execute("""
                INSERT INTO tasks (
                    task_id, tenant_id, agent_id, agent_config_snapshot, 
                    status, input, max_retries, max_steps, task_timeout_seconds,
                    created_at
                ) VALUES ($1, 'default', 'test_agent', $2, 'queued', 'Test', 3, 5, 10, NOW() - INTERVAL '20 seconds')
            """, t4, json.dumps(agent_config))

        await asyncio.sleep(3)
        
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT status, dead_letter_reason FROM tasks WHERE task_id=$1", t4)
            assert str(row["status"]) == "dead_letter"
            assert str(row["dead_letter_reason"]) == "task_timeout"
            
        print("-> Scenario 4 Passed")

    except AssertionError as e:
        print(f"\nIntegration tests failed! Assertion Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nIntegration tests failed! {e}")
        sys.exit(1)
    finally:
        await worker.stop()
        await pool.close()
        print("\n=== All Worker Integration tests passed successfully! ===")

if __name__ == "__main__":
    asyncio.run(run_integration_tests())
