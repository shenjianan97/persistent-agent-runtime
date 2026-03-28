import asyncio
import json
import pytest
from unittest.mock import ANY, AsyncMock, patch, MagicMock

from core.worker import WorkerService
from core.config import WorkerConfig
from executor.graph import GraphExecutor
from langgraph.errors import GraphRecursionError
from checkpointer.postgres import LeaseRevokedException
from tools.errors import ToolExecutionError, ToolTransportError


@pytest.fixture
def mock_worker():
    config = WorkerConfig(worker_id="test-worker", worker_pool_id="shared")
    worker = MagicMock(spec=WorkerService)
    worker.config = config
    
    # Setup pool acquire async context manager
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value="00000000-0000-0000-0000-000000000000")
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn
    worker.pool = MagicMock()
    worker.pool.acquire.return_value = mock_ctx
    worker.pool.execute = AsyncMock()
    worker.pool.fetchval = AsyncMock(return_value="00000000-0000-0000-0000-000000000000")
    worker.pool.fetchrow = AsyncMock(return_value=None)
    worker.pool.fetch = AsyncMock(return_value=[])
    
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
        "agent_id": "test-agent",
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
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    
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
            
            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

            # Verify completed path — completion now uses pool.fetchval with lease guard
            mock_worker.pool.fetchval.assert_called_once_with(
                '''UPDATE tasks
                       SET status='completed',
                           output=$1,
                           last_error_code=NULL,
                           last_error_message=NULL,
                           version=version+1,
                           lease_owner=NULL,
                           lease_expiry=NULL
                       WHERE task_id=$2::uuid
                         AND status='running'
                         AND lease_owner=$3
                       RETURNING task_id''',
                json.dumps({"result": "Final Answer: 4"}),
                task_data["task_id"],
                "test-worker",
            )


@pytest.mark.asyncio
async def test_execute_task_adds_langfuse_callback_and_metadata(mock_worker, task_data):
    mock_worker.config = WorkerConfig(
        worker_id="test-worker",
        worker_pool_id="shared",
        langfuse_enabled=True,
        langfuse_host="http://localhost:3300",
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",
    )
    captured = {}

    with patch("executor.graph.Langfuse") as MockLangfuse:
        mock_langfuse_client = MagicMock(name="langfuse-client")
        mock_langfuse_client.auth_check.return_value = True
        MockLangfuse.return_value = mock_langfuse_client

        executor = GraphExecutor(mock_worker.config, mock_worker.pool)

        with patch.object(executor, "_build_graph") as mock_build:
            mock_graph = MagicMock()
            mock_compiled = MagicMock()
            mock_graph.compile.return_value = mock_compiled
            mock_build.return_value = mock_graph

            with patch("executor.graph.PostgresDurableCheckpointer") as MockCheckpointer, \
                 patch("executor.graph.CallbackHandler") as MockCallbackHandler:
                mock_ckpt = AsyncMock()
                mock_ckpt.aget_tuple.return_value = None
                MockCheckpointer.return_value = mock_ckpt
                callback = MagicMock(name="langfuse-callback")
                MockCallbackHandler.return_value = callback

                async def mock_astream(initial_input, config=None, stream_mode=None):
                    captured["initial_input"] = initial_input
                    captured["config"] = config
                    captured["stream_mode"] = stream_mode
                    yield {"mock": "event"}

                mock_compiled.astream = mock_astream
                mock_state = MagicMock()
                mock_state.values = {"messages": [MagicMock(content="Final Answer: 4")]}
                mock_compiled.aget_state.return_value = mock_state

                await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

    MockLangfuse.assert_called_once_with(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        host="http://localhost:3300",
    )
    MockCallbackHandler.assert_called_once()
    assert captured["config"]["callbacks"] == [callback]
    assert captured["config"]["metadata"] == {
        "langfuse_session_id": str(task_data["task_id"]),
        "langfuse_user_id": task_data["tenant_id"],
        "task_id": str(task_data["task_id"]),
        "agent_id": task_data["agent_id"],
        "tenant_id": task_data["tenant_id"],
    }
    mock_langfuse_client.flush.assert_called_once()


@pytest.mark.asyncio
async def test_build_graph_configures_tool_node_for_expected_tool_errors(mock_worker):
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    cancel_event = asyncio.Event()
    llm = MagicMock()
    llm.ainvoke = AsyncMock()
    llm.bind_tools.return_value = llm

    with patch("executor.providers.create_llm", AsyncMock(return_value=llm)):
        with patch("executor.graph.ToolNode") as MockToolNode:
            await executor._build_graph(
                {
                    "model": "claude-3-5-sonnet-latest",
                    "temperature": 0.5,
                    "allowed_tools": ["calculator"],
                },
                cancel_event=cancel_event,
                task_id="task-123",
            )

    _, kwargs = MockToolNode.call_args
    assert kwargs["handle_tool_errors"] is ToolExecutionError


@pytest.mark.asyncio
async def test_await_or_cancel_interrupts_long_running_operation(mock_worker):
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    cancel_event = asyncio.Event()
    started = asyncio.Event()

    async def slow_operation():
        started.set()
        await asyncio.sleep(10)

    pending = asyncio.create_task(
        executor._await_or_cancel(
            slow_operation(),
            cancel_event,
            task_id="task-123",
            operation="agent",
        )
    )
    await started.wait()
    cancel_event.set()

    with pytest.raises(LeaseRevokedException):
        await pending


@pytest.mark.asyncio
async def test_execute_task_does_not_persist_legacy_checkpoint_cost(mock_worker, task_data):
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)

    with patch.object(executor, "_build_graph") as mock_build:
        mock_graph = MagicMock()
        mock_compiled = AsyncMock()
        mock_graph.compile.return_value = mock_compiled
        mock_build.return_value = mock_graph

        with patch("executor.graph.PostgresDurableCheckpointer") as MockCheckpointer:
            mock_ckpt = AsyncMock()
            mock_ckpt.aget_tuple.return_value = None
            MockCheckpointer.return_value = mock_ckpt

            async def mock_astream(*args, **kwargs):
                yield {"mock": "event"}
            mock_compiled.astream = mock_astream

            mock_state = MagicMock()
            mock_state.values = {"messages": [MagicMock(content="Final Answer: 4")]}
            mock_compiled.aget_state.return_value = mock_state
            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

            mock_worker.pool.execute.assert_not_called()
            mock_worker.pool.fetch.assert_not_called()
            mock_worker.pool.fetchrow.assert_not_called()
            completion_args, _ = mock_worker.pool.fetchval.call_args
            assert "UPDATE tasks" in completion_args[0]
            assert "status='completed'" in completion_args[0]


@pytest.mark.asyncio
async def test_timeout_dead_letter(mock_worker, task_data):
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
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
            
            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

            # Verify dead letter logic — now uses conn.fetchval with lease guard
            mock_worker.pool.acquire.return_value.__aenter__.return_value.fetchval.assert_called_with(
                '''UPDATE tasks
                   SET status='dead_letter',
                       dead_letter_reason=$1,
                       last_error_message=$2,
                       last_error_code=$3,
                       last_worker_id=$4,
                       dead_lettered_at=NOW(),
                       version=version+1,
                       lease_owner=NULL,
                       lease_expiry=NULL
                   WHERE task_id=$5::uuid
                     AND status='running'
                     AND lease_owner=$6
                   RETURNING task_id''',
                "task_timeout",
                "Execution exceeded task logic timeout",
                "task_timeout",
                "test-worker",
                task_data["task_id"],
                "test-worker",
            )


@pytest.mark.asyncio
async def test_retryable_error(mock_worker, task_data):
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    
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
            
            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)
            
            # Verify retry logic
            mock_worker.pool.acquire.return_value.__aenter__.return_value.execute.assert_any_call(
                "SELECT pg_notify('new_task', $1)",
                "shared"
            )


@pytest.mark.asyncio
async def test_non_retryable_error(mock_worker, task_data):
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    
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
            
            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

            # Verify dead letter logic — now uses conn.fetchval with lease guard
            mock_worker.pool.acquire.return_value.__aenter__.return_value.fetchval.assert_called_with(
                '''UPDATE tasks
                   SET status='dead_letter',
                       dead_letter_reason=$1,
                       last_error_message=$2,
                       last_error_code=$3,
                       last_worker_id=$4,
                       dead_lettered_at=NOW(),
                       version=version+1,
                       lease_owner=NULL,
                       lease_expiry=NULL
                   WHERE task_id=$5::uuid
                     AND status='running'
                     AND lease_owner=$6
                   RETURNING task_id''',
                "non_retryable_error",
                "pydantic validation error: invalid property",
                "fatal_error",
                "test-worker",
                task_data["task_id"],
                "test-worker",
            )


@pytest.mark.asyncio
async def test_graph_recursion_error(mock_worker, task_data):
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    
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
            
            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

            # Verify dead letter logic — now uses conn.fetchval with lease guard
            mock_worker.pool.acquire.return_value.__aenter__.return_value.fetchval.assert_called_with(
                '''UPDATE tasks
                   SET status='dead_letter',
                       dead_letter_reason=$1,
                       last_error_message=$2,
                       last_error_code=$3,
                       last_worker_id=$4,
                       dead_lettered_at=NOW(),
                       version=version+1,
                       lease_owner=NULL,
                       lease_expiry=NULL
                   WHERE task_id=$5::uuid
                     AND status='running'
                     AND lease_owner=$6
                   RETURNING task_id''',
                "max_steps_exceeded",
                "Execution exceeded max_steps (5)",
                "max_steps_exceeded",
                "test-worker",
                task_data["task_id"],
                "test-worker",
            )


@pytest.mark.asyncio
async def test_retries_exhausted(mock_worker, task_data):
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
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
            
            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

            # Verify dead letter logic with retries_exhausted — now uses conn.fetchval with lease guard
            mock_worker.pool.acquire.return_value.__aenter__.return_value.fetchval.assert_called_with(
                '''UPDATE tasks
                   SET status='dead_letter',
                       dead_letter_reason=$1,
                       last_error_message=$2,
                       last_error_code=$3,
                       last_worker_id=$4,
                       dead_lettered_at=NOW(),
                       version=version+1,
                       lease_owner=NULL,
                       lease_expiry=NULL
                   WHERE task_id=$5::uuid
                     AND status='running'
                     AND lease_owner=$6
                   RETURNING task_id''',
                "retries_exhausted",
                "Max retries reached. Last error: 503 Service Unavailable",
                "retries_exhausted",
                "test-worker",
                task_data["task_id"],
                "test-worker",
            )


@pytest.mark.asyncio
async def test_cancellation_awareness(mock_worker, task_data):
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    
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
            
            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)
            
            # Verify that pool.execute and acquire.execute were NOT called (no status updates)
            # No completed and no dead letter should be written by the executor.
            mock_worker.pool.execute.assert_not_called()
            mock_worker.pool.acquire.return_value.__aenter__.return_value.execute.assert_not_called()


@pytest.mark.asyncio
async def test_read_url_failure_preserves_failing_url_on_retryable_requeue(mock_worker, task_data):
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)

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
                raise ToolTransportError("URL fetch request failed for https://bad.example/fail: network down")
                yield {}

            mock_compiled.astream = failing_astream

            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

    # Retry requeue now uses conn.fetchval with lease guard
    mock_worker.pool.acquire.return_value.__aenter__.return_value.fetchval.assert_any_call(
        '''UPDATE tasks
                   SET status='queued',
                       retry_count=$1,
                       retry_after=$2,
                       retry_history=COALESCE(retry_history, '[]'::jsonb) || jsonb_build_array(NOW()),
                       last_error_code='retryable_error',
                       last_error_message=$3,
                       version=version+1,
                       lease_owner=NULL,
                       lease_expiry=NULL
                   WHERE task_id=$4::uuid
                     AND status='running'
                     AND lease_owner=$5
                   RETURNING task_id''',
        1,
        ANY,
        "URL fetch request failed for https://bad.example/fail: network down",
        task_data["task_id"],
        "test-worker",
    )


def test_tool_transport_error_is_retryable(mock_worker):
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)

    assert executor._is_retryable_error(
        ToolTransportError("URL fetch request failed for https://bad.example/fail: network down")
    ) is True


def test_rate_limit_with_invalid_in_message_is_retryable(mock_worker):
    """Issue #14: 'invalid request rate exceeded' was previously dead-lettered because
    the 'invalid' string check ran before the 429/rate-limit check."""
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)

    assert executor._is_retryable_error(Exception("invalid request rate exceeded")) is True
    assert executor._is_retryable_error(Exception("429 Too Many Requests")) is True
    assert executor._is_retryable_error(Exception("rate limit reached")) is True


def test_real_validation_errors_are_not_retryable(mock_worker):
    """Ensure the retryable-first ordering doesn't accidentally make validation errors retryable."""
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)

    assert executor._is_retryable_error(ValueError("pydantic validation error")) is False
    assert executor._is_retryable_error(ValueError("invalid schema property")) is False
    assert executor._is_retryable_error(ValueError("unsupported model")) is False


@pytest.mark.asyncio
async def test_completion_stolen_lease_does_not_crash(mock_worker, task_data):
    """Issue #12: if the lease was stolen before completion, fetchval returns None.
    The executor must log a warning and return cleanly instead of crashing."""
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    # Simulate lease stolen: fetchval returns None (0 rows updated)
    mock_worker.pool.fetchval = AsyncMock(return_value=None)

    with patch.object(executor, "_build_graph") as mock_build:
        mock_graph = MagicMock()
        mock_compiled = AsyncMock()
        mock_graph.compile.return_value = mock_compiled
        mock_build.return_value = mock_graph

        with patch("executor.graph.PostgresDurableCheckpointer") as MockCheckpointer:
            mock_ckpt = AsyncMock()
            mock_ckpt.aget_tuple.return_value = None
            MockCheckpointer.return_value = mock_ckpt

            async def mock_astream(*args, **kwargs):
                yield {"mock": "event"}
            mock_compiled.astream = mock_astream

            mock_state = MagicMock()
            mock_state.values = {"messages": [MagicMock(content="Answer")]}
            mock_compiled.aget_state.return_value = mock_state

            # Should not raise
            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

        # fetchval was called (attempted the update) but returned None — no exception
        mock_worker.pool.fetchval.assert_called_once()


@pytest.mark.asyncio
async def test_dead_letter_stolen_lease_does_not_crash(mock_worker, task_data):
    """Issue #12: if the lease was stolen before dead-lettering, fetchval returns None.
    The executor must log a warning and return cleanly."""
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    mock_worker.pool.acquire.return_value.__aenter__.return_value.fetchval = AsyncMock(return_value=None)

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
                raise ValueError("unsupported model type")
                yield {}
            mock_compiled.astream = failing_astream

            # Should not raise even though fetchval returns None
            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

        mock_worker.pool.acquire.return_value.__aenter__.return_value.fetchval.assert_called_once()


@pytest.mark.asyncio
async def test_retry_requeue_stolen_lease_skips_notify(mock_worker, task_data):
    """Issue #12: if the lease was stolen before retry-requeue, fetchval returns None.
    The executor must skip the pg_notify and return cleanly."""
    task_data["retry_count"] = 0
    task_data["max_retries"] = 3
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    # Simulate lease stolen on the retry-requeue UPDATE
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn
    mock_worker.pool.acquire.return_value = mock_ctx

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
                raise ConnectionError("connection reset")
                yield {}
            mock_compiled.astream = failing_astream

            # Should not raise
            await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

    # fetchval was called (retry-requeue UPDATE) but returned None
    mock_conn.fetchval.assert_called_once()
    # pg_notify should NOT have been called since the update was skipped
    mock_conn.execute.assert_not_called()
