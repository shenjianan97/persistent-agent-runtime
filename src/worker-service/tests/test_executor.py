import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from core.worker import WorkerService
from core.config import WorkerConfig
from executor.graph import GraphExecutor
from langgraph.errors import GraphRecursionError
from checkpointer.postgres import LeaseRevokedException


@pytest.fixture
def mock_worker():
    config = WorkerConfig(worker_id="test-worker", worker_pool_id="shared")
    worker = MagicMock(spec=WorkerService)
    worker.config = config
    
    # Setup pool acquire async context manager
    mock_conn = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn
    worker.pool = MagicMock()
    worker.pool.acquire.return_value = mock_ctx
    worker.pool.execute = AsyncMock()
    
    worker.heartbeat = MagicMock()
    worker.heartbeat.stop_heartbeat = AsyncMock()
    # Heartbeat handle
    handle = MagicMock()
    handle.cancel_event = asyncio.Event()
    worker.heartbeat.start_heartbeat = MagicMock(return_value=handle)
    return worker


@pytest.fixture
def task_data():
    return {
        "task_id": "00000000-0000-0000-0000-000000000000",
        "tenant_id": "test-tenant",
        "agent_config_snapshot": json.dumps({
            "model": "claude-3-5-sonnet-latest",
            "temperature": 0.5,
            "allowed_tools": ["calculator"]
        }),
        "input": "What is 2+2?",
        "max_steps": 5,
        "task_timeout_seconds": 10,
        "retry_count": 0,
        "max_retries": 3
    }


@pytest.mark.asyncio
async def test_completion_path(mock_worker, task_data):
    executor = GraphExecutor(mock_worker)
    
    # Mock compile and building
    with patch.object(executor, "_build_graph") as mock_build:
        mock_graph = MagicMock()
        mock_compiled = AsyncMock()
        mock_graph.compile.return_value = mock_compiled
        mock_build.return_value = mock_graph
        
        # Mock Checkpointer
        with patch("executor.graph.PostgresDurableCheckpointer") as MockCheckpointer:
            mock_ckpt = AsyncMock()
            mock_ckpt.aget_tuple.return_value = None
            MockCheckpointer.return_value = mock_ckpt
            
            # Mock astream to yield nothing (it just ends)
            async def mock_astream(*args, **kwargs):
                yield {"mock": "event"}
            mock_compiled.astream = mock_astream
            
            # Mock final state
            mock_state = MagicMock()
            mock_state.values = {"messages": [MagicMock(content="Final Answer: 4")]}
            mock_compiled.aget_state.return_value = mock_state
            
            await executor.execute_task(task_data)
            
            # Verify completed path
            assert mock_worker.pool.execute.call_count == 1
            args, _ = mock_worker.pool.execute.call_args
            assert "UPDATE tasks" in args[0]
            assert "status='completed'" in args[0]
            assert args[1] == json.dumps({"result": "Final Answer: 4"})
            assert args[2] == task_data["task_id"]


@pytest.mark.asyncio
async def test_timeout_dead_letter(mock_worker, task_data):
    executor = GraphExecutor(mock_worker)
    task_data["task_timeout_seconds"] = 1
    
    with patch.object(executor, "_build_graph") as mock_build:
        mock_graph = MagicMock()
        mock_compiled = AsyncMock()
        mock_graph.compile.return_value = mock_compiled
        mock_build.return_value = mock_graph
        
        with patch("executor.graph.PostgresDurableCheckpointer") as MockCheckpointer:
            mock_ckpt = AsyncMock()
            mock_ckpt.aget_tuple.return_value = None
            MockCheckpointer.return_value = mock_ckpt
            async def slow_astream(*args, **kwargs):
                await asyncio.sleep(2)
                yield {}
            mock_compiled.astream = slow_astream
            
            await executor.execute_task(task_data)
            
            # Verify dead letter logic
            mock_worker.pool.acquire.return_value.__aenter__.return_value.execute.assert_called_with(
                '''UPDATE tasks 
                   SET status='dead_letter', 
                       dead_letter_reason=$1, 
                       last_error_message=$2,
                       last_worker_id=$3,
                       dead_lettered_at=NOW(),
                       version=version+1,
                       lease_owner=NULL,
                       lease_expiry=NULL
                   WHERE task_id=$4::uuid''',
                "task_timeout",
                "Execution exceeded task logic timeout",
                "test-worker",
                task_data["task_id"]
            )


@pytest.mark.asyncio
async def test_retryable_error(mock_worker, task_data):
    executor = GraphExecutor(mock_worker)
    
    with patch.object(executor, "_build_graph") as mock_build:
        mock_graph = MagicMock()
        mock_compiled = AsyncMock()
        mock_graph.compile.return_value = mock_compiled
        mock_build.return_value = mock_graph
        
        with patch("executor.graph.PostgresDurableCheckpointer") as MockCheckpointer:
            mock_ckpt = AsyncMock()
            mock_ckpt.aget_tuple.return_value = None
            MockCheckpointer.return_value = mock_ckpt
            async def failing_astream(*args, **kwargs):
                raise ConnectionError("503 Service Unavailable")
                yield {}
            mock_compiled.astream = failing_astream
            
            await executor.execute_task(task_data)
            
            # Verify retry logic
            mock_worker.pool.acquire.return_value.__aenter__.return_value.execute.assert_any_call(
                "SELECT pg_notify('new_task', $1)",
                "shared"
            )


@pytest.mark.asyncio
async def test_non_retryable_error(mock_worker, task_data):
    executor = GraphExecutor(mock_worker)
    
    with patch.object(executor, "_build_graph") as mock_build:
        mock_graph = MagicMock()
        mock_compiled = AsyncMock()
        mock_graph.compile.return_value = mock_compiled
        mock_build.return_value = mock_graph
        
        with patch("executor.graph.PostgresDurableCheckpointer") as MockCheckpointer:
            mock_ckpt = AsyncMock()
            mock_ckpt.aget_tuple.return_value = None
            MockCheckpointer.return_value = mock_ckpt
            async def failing_astream(*args, **kwargs):
                raise ValueError("pydantic validation error: invalid property")
                yield {}
            mock_compiled.astream = failing_astream
            
            await executor.execute_task(task_data)
            
            # Verify dead letter logic
            mock_worker.pool.acquire.return_value.__aenter__.return_value.execute.assert_called_with(
                '''UPDATE tasks 
                   SET status='dead_letter', 
                       dead_letter_reason=$1, 
                       last_error_message=$2,
                       last_worker_id=$3,
                       dead_lettered_at=NOW(),
                       version=version+1,
                       lease_owner=NULL,
                       lease_expiry=NULL
                   WHERE task_id=$4::uuid''',
                "non_retryable_error",
                "pydantic validation error: invalid property",
                "test-worker",
                task_data["task_id"]
            )


@pytest.mark.asyncio
async def test_graph_recursion_error(mock_worker, task_data):
    executor = GraphExecutor(mock_worker)
    
    with patch.object(executor, "_build_graph") as mock_build:
        mock_graph = MagicMock()
        mock_compiled = AsyncMock()
        mock_graph.compile.return_value = mock_compiled
        mock_build.return_value = mock_graph
        
        with patch("executor.graph.PostgresDurableCheckpointer") as MockCheckpointer:
            mock_ckpt = AsyncMock()
            mock_ckpt.aget_tuple.return_value = None
            MockCheckpointer.return_value = mock_ckpt
            async def failing_astream(*args, **kwargs):
                raise GraphRecursionError("Recursion limit exceeded")
                yield {}
            mock_compiled.astream = failing_astream
            
            await executor.execute_task(task_data)
            
            # Verify dead letter logic
            mock_worker.pool.acquire.return_value.__aenter__.return_value.execute.assert_called_with(
                '''UPDATE tasks 
                   SET status='dead_letter', 
                       dead_letter_reason=$1, 
                       last_error_message=$2,
                       last_worker_id=$3,
                       dead_lettered_at=NOW(),
                       version=version+1,
                       lease_owner=NULL,
                       lease_expiry=NULL
                   WHERE task_id=$4::uuid''',
                "max_steps_exceeded",
                "Execution exceeded max_steps (5)",
                "test-worker",
                task_data["task_id"]
            )


@pytest.mark.asyncio
async def test_retries_exhausted(mock_worker, task_data):
    executor = GraphExecutor(mock_worker)
    task_data["retry_count"] = 3
    task_data["max_retries"] = 3
    
    with patch.object(executor, "_build_graph") as mock_build:
        mock_graph = MagicMock()
        mock_compiled = AsyncMock()
        mock_graph.compile.return_value = mock_compiled
        mock_build.return_value = mock_graph
        
        with patch("executor.graph.PostgresDurableCheckpointer") as MockCheckpointer:
            mock_ckpt = AsyncMock()
            mock_ckpt.aget_tuple.return_value = None
            MockCheckpointer.return_value = mock_ckpt
            async def failing_astream(*args, **kwargs):
                raise ConnectionError("503 Service Unavailable")
                yield {}
            mock_compiled.astream = failing_astream
            
            await executor.execute_task(task_data)
            
            # Verify dead letter logic with retries_exhausted
            mock_worker.pool.acquire.return_value.__aenter__.return_value.execute.assert_called_with(
                '''UPDATE tasks 
                   SET status='dead_letter', 
                       dead_letter_reason=$1, 
                       last_error_message=$2,
                       last_worker_id=$3,
                       dead_lettered_at=NOW(),
                       version=version+1,
                       lease_owner=NULL,
                       lease_expiry=NULL
                   WHERE task_id=$4::uuid''',
                "retries_exhausted",
                "Max retries reached. Last error: 503 Service Unavailable",
                "test-worker",
                task_data["task_id"]
            )


@pytest.mark.asyncio
async def test_cancellation_awareness(mock_worker, task_data):
    executor = GraphExecutor(mock_worker)
    
    with patch.object(executor, "_build_graph") as mock_build:
        mock_graph = MagicMock()
        mock_compiled = AsyncMock()
        mock_graph.compile.return_value = mock_compiled
        mock_build.return_value = mock_graph
        
        with patch("executor.graph.PostgresDurableCheckpointer") as MockCheckpointer:
            mock_ckpt = AsyncMock()
            mock_ckpt.aget_tuple.return_value = None
            MockCheckpointer.return_value = mock_ckpt
            
            # Retrieve the handle created by the mock_worker
            handle = mock_worker.heartbeat.start_heartbeat.return_value
            
            async def cancelling_astream(*args, **kwargs):
                # Simulate lease revocation during execution
                handle.cancel_event.set()
                yield {"mock": "event"}
                
            mock_compiled.astream = cancelling_astream
            
            await executor.execute_task(task_data)
            
            # Verify that pool.execute and acquire.execute were NOT called (no status updates)
            # No completed and no dead letter should be written by the executor.
            mock_worker.pool.execute.assert_not_called()
            mock_worker.pool.acquire.return_value.__aenter__.return_value.execute.assert_not_called()
