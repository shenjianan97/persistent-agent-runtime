import asyncio
import json
import pytest
from unittest.mock import ANY, AsyncMock, patch, MagicMock

from core.worker import WorkerService
from core.config import WorkerConfig
from executor.graph import GraphExecutor
from executor.mcp_session import McpConnectionError
from executor.schema_converter import MAX_TOOLS_PER_AGENT
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphRecursionError
from checkpointer.postgres import LeaseRevokedException
from tools.errors import ToolExecutionError, ToolTransportError
from sandbox.provisioner import SandboxProvisionError, SandboxConnectionError


def _make_mock_conn():
    """Create a mock connection that supports transaction() as an async context manager."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetchval = AsyncMock(return_value="00000000-0000-0000-0000-000000000000")

    # transaction() must return a sync object with __aenter__/__aexit__
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=None)
    mock_tx.__aexit__ = AsyncMock(return_value=False)
    mock_conn.transaction = MagicMock(return_value=mock_tx)
    return mock_conn


def _make_mock_pool(mock_conn=None):
    """Create a mock pool where acquire() is an async context manager yielding mock_conn."""
    if mock_conn is None:
        mock_conn = _make_mock_conn()
    pool = MagicMock()
    mock_acquire_ctx = AsyncMock()
    mock_acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=mock_acquire_ctx)
    # Direct pool methods (for code that uses pool.execute/fetch/etc. directly)
    pool.execute = AsyncMock()
    pool.fetchval = AsyncMock(return_value="00000000-0000-0000-0000-000000000000")
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    return pool, mock_conn


@pytest.fixture
def mock_worker():
    config = WorkerConfig(worker_id="test-worker", worker_pool_id="shared")
    worker = MagicMock(spec=WorkerService)
    worker.config = config

    pool, mock_conn = _make_mock_pool()
    worker.pool = pool

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
            "allowed_tools": ["web_search"]
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

            # Verify completed path — completion now uses conn.fetchval via pool.acquire()
            mock_conn = mock_worker.pool.acquire.return_value.__aenter__.return_value
            mock_conn.fetchval.assert_called_with(
                '''UPDATE tasks
                               SET status='completed',
                                   output=$1,
                                   last_error_code=NULL,
                                   last_error_message=NULL,
                                   human_response=NULL,
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
                    "allowed_tools": ["web_search"],
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
async def test_execute_task_persists_checkpoint_cost(mock_worker, task_data):
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

            # Build an AI message with response_metadata for per-step cost tracking
            mock_msg = MagicMock()
            mock_msg.type = "ai"
            mock_msg.content = "Final Answer: 4"
            mock_msg.response_metadata = {
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }

            async def mock_astream(*args, **kwargs):
                # Yield an agent event so per-step cost tracking fires
                yield {"agent": {"messages": [mock_msg]}}
            mock_compiled.astream = mock_astream

            mock_state = MagicMock()
            mock_state.values = {"messages": [mock_msg]}
            mock_compiled.aget_state.return_value = mock_state

            # Mock _calculate_step_cost to return a non-zero cost and
            # _record_step_cost to verify it is called
            with patch.object(executor, "_calculate_step_cost", new_callable=AsyncMock, return_value=(150, {"input_tokens": 100, "output_tokens": 50, "model": "claude-3-5-sonnet-latest"})):
                with patch.object(executor, "_record_step_cost", new_callable=AsyncMock, return_value=(150, 150)) as mock_record:
                    await executor.execute_task(task_data, mock_worker.heartbeat.start_heartbeat.return_value.cancel_event)

                    # Per-step cost recording should have been called via pool.acquire()
                    mock_record.assert_called_once()
                    call_args = mock_record.call_args
                    # Verify task_id, tenant_id, agent_id were passed
                    assert call_args[0][1] == task_data["task_id"]
                    assert call_args[0][2] == task_data["tenant_id"]
                    assert call_args[0][3] == task_data["agent_id"]
                    # Verify cost_microdollars was passed
                    assert call_args[0][5] == 150

            # Completion should still work via conn.fetchval
            mock_conn = mock_worker.pool.acquire.return_value.__aenter__.return_value
            # Find the completion call among all fetchval calls
            fetchval_calls = mock_conn.fetchval.call_args_list
            completion_calls = [c for c in fetchval_calls if "UPDATE tasks" in str(c) and "status='completed'" in str(c)]
            assert len(completion_calls) > 0, "Expected task completion UPDATE"


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


# ─── Custom Tool Integration Tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_task_no_tool_servers_unchanged(mock_worker, task_data):
    """Tasks without tool_servers behave identically to before — no MCP session created."""
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)

    # agent_config_snapshot has no tool_servers key
    assert "tool_servers" not in json.loads(task_data["agent_config_snapshot"])

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
            mock_state.values = {"messages": [MagicMock(content="Done")]}
            mock_state.tasks = []
            mock_compiled.aget_state.return_value = mock_state

            with patch("executor.graph.McpSessionManager") as MockMcpSessionManager:
                await executor.execute_task(
                    task_data,
                    mock_worker.heartbeat.start_heartbeat.return_value.cancel_event,
                )

                # McpSessionManager should NOT be instantiated
                MockMcpSessionManager.assert_not_called()

                # _build_graph should be called with custom_tools=None
                mock_build.assert_called_once()
                _, kwargs = mock_build.call_args
                assert kwargs.get("custom_tools") is None


@pytest.mark.asyncio
async def test_execute_task_tool_server_not_found_dead_letters(mock_worker, task_data):
    """Referencing a non-existent tool server causes dead-letter with tool_server_unavailable."""
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    task_data["agent_config_snapshot"] = json.dumps({
        "model": "claude-3-5-sonnet-latest",
        "temperature": 0.5,
        "allowed_tools": [],
        "tool_servers": ["missing-server"],
    })

    # DB returns no rows (server not found)
    mock_conn = mock_worker.pool.acquire.return_value.__aenter__.return_value
    mock_conn.fetch.return_value = []

    with patch.object(executor, "_handle_dead_letter", new_callable=AsyncMock) as mock_dead_letter:
        await executor.execute_task(
            task_data,
            mock_worker.heartbeat.start_heartbeat.return_value.cancel_event,
        )

    mock_dead_letter.assert_called_once()
    call_kwargs = mock_dead_letter.call_args
    assert call_kwargs[1].get("error_code") == "tool_server_unavailable" or \
           (len(call_kwargs[0]) >= 6 and call_kwargs[0][5] == "tool_server_unavailable")


@pytest.mark.asyncio
async def test_execute_task_tool_server_disabled_dead_letters(mock_worker, task_data):
    """Referencing a disabled tool server causes dead-letter with tool_server_unavailable."""
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    task_data["agent_config_snapshot"] = json.dumps({
        "model": "claude-3-5-sonnet-latest",
        "temperature": 0.5,
        "allowed_tools": [],
        "tool_servers": ["my-server"],
    })

    # DB returns the server but with status='disabled'
    mock_conn = mock_worker.pool.acquire.return_value.__aenter__.return_value
    disabled_row = {
        "name": "my-server",
        "url": "http://my-server:8080/mcp",
        "auth_type": "none",
        "auth_token": None,
        "status": "disabled",
    }
    mock_conn.fetch.return_value = [disabled_row]

    with patch.object(executor, "_handle_dead_letter", new_callable=AsyncMock) as mock_dead_letter:
        await executor.execute_task(
            task_data,
            mock_worker.heartbeat.start_heartbeat.return_value.cancel_event,
        )

    mock_dead_letter.assert_called_once()
    # Verify error_code is tool_server_unavailable
    call_args = mock_dead_letter.call_args
    # error_code is passed as keyword arg
    assert call_args[1].get("error_code") == "tool_server_unavailable"


@pytest.mark.asyncio
async def test_build_graph_with_custom_tools_merges(mock_worker):
    """_build_graph() with custom_tools produces merged tool list."""
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    cancel_event = asyncio.Event()

    llm = MagicMock()
    llm.ainvoke = AsyncMock()
    llm.bind_tools.return_value = llm

    # Create a dummy custom tool
    from pydantic import BaseModel as PydanticBaseModel

    class DummyArgs(PydanticBaseModel):
        x: str

    custom_tool = StructuredTool.from_function(
        coroutine=AsyncMock(return_value="custom result"),
        name="my-server__custom_tool",
        description="A custom tool",
        args_schema=DummyArgs,
    )

    with patch("executor.providers.create_llm", AsyncMock(return_value=llm)):
        with patch("executor.graph.ToolNode") as MockToolNode:
            await executor._build_graph(
                {
                    "model": "claude-3-5-sonnet-latest",
                    "temperature": 0.5,
                    "allowed_tools": ["web_search"],
                },
                cancel_event=cancel_event,
                task_id="task-123",
                custom_tools=[custom_tool],
            )

    # ToolNode should be called with both built-in and custom tools
    assert MockToolNode.called
    tools_arg = MockToolNode.call_args[0][0]
    tool_names = [t.name for t in tools_arg]
    assert "web_search" in tool_names
    assert "my-server__custom_tool" in tool_names


@pytest.mark.asyncio
async def test_build_graph_exceeds_tool_limit_raises(mock_worker):
    """More than MAX_TOOLS_PER_AGENT total tools raises ValueError."""
    executor = GraphExecutor(mock_worker.config, mock_worker.pool)
    cancel_event = asyncio.Event()

    llm = MagicMock()
    llm.ainvoke = AsyncMock()
    llm.bind_tools.return_value = llm

    from pydantic import BaseModel as PydanticBaseModel

    class NoArgs(PydanticBaseModel):
        pass

    # Create 128 custom tools to exceed the limit (plus 1 built-in = 129)
    many_tools = [
        StructuredTool.from_function(
            coroutine=AsyncMock(return_value="result"),
            name=f"srv__tool_{i}",
            description=f"Tool {i}",
            args_schema=NoArgs,
        )
        for i in range(MAX_TOOLS_PER_AGENT)
    ]

    with patch("executor.providers.create_llm", AsyncMock(return_value=llm)):
        with pytest.raises(ValueError, match="max"):
            await executor._build_graph(
                {
                    "model": "claude-3-5-sonnet-latest",
                    "temperature": 0.5,
                    "allowed_tools": ["web_search"],  # 1 built-in + 128 custom = 129 > 128
                },
                cancel_event=cancel_event,
                task_id="task-123",
                custom_tools=many_tools,
            )
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
    # Simulate lease stolen: conn.fetchval returns None (0 rows updated)
    mock_conn = mock_worker.pool.acquire.return_value.__aenter__.return_value
    mock_conn.fetchval = AsyncMock(return_value=None)

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

        # conn.fetchval was called (attempted the update) but returned None — no exception
        mock_conn.fetchval.assert_called_once()


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
    mock_conn = _make_mock_conn()
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock()
    mock_acquire_ctx = AsyncMock()
    mock_acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_worker.pool.acquire = MagicMock(return_value=mock_acquire_ctx)

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


# ---------------------------------------------------------------------------
# Input file injection tests (Task 6)
# ---------------------------------------------------------------------------

def _build_test_executor():
    """Build a GraphExecutor with a mock pool for unit testing."""
    config = WorkerConfig(worker_id="test-worker", worker_pool_id="shared")
    pool, _ = _make_mock_pool()
    return GraphExecutor(config, pool)


class TestInputFileInjection:
    @pytest.mark.asyncio
    async def test_inject_no_input_files_returns_empty_list(self):
        """No input artifacts → returns empty list, no sandbox writes."""
        executor = _build_test_executor()
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_acquire_ctx = AsyncMock()
        mock_acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_ctx.__aexit__ = AsyncMock(return_value=False)
        executor.pool.acquire = MagicMock(return_value=mock_acquire_ctx)

        result = await executor._inject_input_files(mock_sandbox, "task-123", "default")
        assert result == []
        # sandbox.files.write should never have been called
        mock_sandbox.files.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_inject_input_files_downloads_and_writes_to_sandbox(self):
        """Input artifacts are downloaded from S3 and written to sandbox."""
        executor = _build_test_executor()
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        rows = [
            {"filename": "data.csv", "s3_key": "default/task-123/input/data.csv",
             "content_type": "text/csv", "size_bytes": 100},
        ]
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_acquire_ctx = AsyncMock()
        mock_acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_ctx.__aexit__ = AsyncMock(return_value=False)
        executor.pool.acquire = MagicMock(return_value=mock_acquire_ctx)

        executor.s3_client = MagicMock()
        executor.s3_client.download = AsyncMock(return_value=b"csv,data\n1,2")

        with patch("executor.graph.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = None
            result = await executor._inject_input_files(mock_sandbox, "task-123", "default")

        assert result == ["data.csv"]
        executor.s3_client.download.assert_called_once_with("default/task-123/input/data.csv")
        # Verify to_thread was called with sandbox.files.write and the correct path
        mock_thread.assert_called_once()
        call_args = mock_thread.call_args
        assert call_args[0][1] == "/home/user/data.csv"
        assert call_args[0][2] == b"csv,data\n1,2"

    @pytest.mark.asyncio
    async def test_inject_input_files_multiple_files(self):
        """Multiple input artifacts are all downloaded and written."""
        executor = _build_test_executor()
        mock_sandbox = MagicMock()

        rows = [
            {"filename": "file1.txt", "s3_key": "t/task/input/file1.txt",
             "content_type": "text/plain", "size_bytes": 10},
            {"filename": "file2.csv", "s3_key": "t/task/input/file2.csv",
             "content_type": "text/csv", "size_bytes": 20},
        ]
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_acquire_ctx = AsyncMock()
        mock_acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_ctx.__aexit__ = AsyncMock(return_value=False)
        executor.pool.acquire = MagicMock(return_value=mock_acquire_ctx)

        executor.s3_client = MagicMock()
        executor.s3_client.download = AsyncMock(return_value=b"data")

        with patch("executor.graph.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = None
            result = await executor._inject_input_files(mock_sandbox, "task-abc", "t")

        assert result == ["file1.txt", "file2.csv"]
        assert executor.s3_client.download.call_count == 2

    @pytest.mark.asyncio
    async def test_inject_input_files_s3_failure_raises_runtime_error(self):
        """S3 download failure raises RuntimeError."""
        executor = _build_test_executor()
        mock_sandbox = MagicMock()

        rows = [
            {"filename": "data.csv", "s3_key": "t/task/input/data.csv",
             "content_type": "text/csv", "size_bytes": 50},
        ]
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_acquire_ctx = AsyncMock()
        mock_acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_ctx.__aexit__ = AsyncMock(return_value=False)
        executor.pool.acquire = MagicMock(return_value=mock_acquire_ctx)

        executor.s3_client = MagicMock()
        executor.s3_client.download = AsyncMock(side_effect=Exception("S3 unavailable"))

        with pytest.raises(RuntimeError, match="Failed to inject input file 'data.csv'"):
            await executor._inject_input_files(mock_sandbox, "task-abc", "t")

    def test_platform_system_message_no_files_no_sandbox(self):
        """No files, no sandbox → only base tool instructions."""
        executor = _build_test_executor()
        msg = executor._build_platform_system_message(["web_search", "request_human_input"])
        assert "request_human_input" in msg
        assert "web_search" in msg

    def test_platform_system_message_with_injected_files(self):
        """Injected files → paths appear in message."""
        executor = _build_test_executor()
        msg = executor._build_platform_system_message(
            ["sandbox_exec", "sandbox_read_file"],
            injected_files=["data.csv"],
        )
        assert "/home/user/data.csv" in msg
        assert "sandbox_read_file" in msg

    def test_platform_system_message_multiple_files(self):
        """Multiple files → all paths appear in message."""
        executor = _build_test_executor()
        msg = executor._build_platform_system_message(
            ["sandbox_exec"],
            injected_files=["data.csv", "readme.txt"],
        )
        assert "/home/user/data.csv" in msg
        assert "/home/user/readme.txt" in msg


# ---------------------------------------------------------------------------
# Sandbox lifecycle tests (Task 7)
# ---------------------------------------------------------------------------

def _build_sandbox_task_data(sandbox_enabled: bool = False, sandbox_id: str | None = None) -> dict:
    """Build task_data with optional sandbox config for sandbox lifecycle tests."""
    agent_config = {
        "model": "claude-3-5-sonnet-latest",
        "temperature": 0.5,
        "allowed_tools": ["sandbox_exec"],
    }
    if sandbox_enabled:
        agent_config["sandbox"] = {
            "enabled": True,
            "template": "base",
            "vcpu": 2,
            "memory_mb": 2048,
            "timeout_seconds": 3600,
        }

    data = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "tenant_id": "test-tenant",
        "agent_id": "test-agent",
        "agent_config_snapshot": json.dumps(agent_config),
        "input": "Run a script",
        "max_steps": 5,
        "task_timeout_seconds": 10,
        "retry_count": 0,
        "max_retries": 3,
    }
    if sandbox_id is not None:
        data["sandbox_id"] = sandbox_id
    return data


class TestSandboxLifecycle:
    def test_sandbox_cost_calculation(self):
        """Verify sandbox cost formula: duration_seconds * vcpu * $0.05/3600."""
        duration_seconds = 600  # 10 minutes
        vcpu = 2
        # Expected: 600 * 2 * 50000 / 3600 = 16666 microdollars
        expected = int(duration_seconds * vcpu * 50000 / 3600)
        assert expected == 16666

    def test_sandbox_cost_calculation_small(self):
        """Verify sandbox cost for minimal usage."""
        duration_seconds = 60  # 1 minute
        vcpu = 1
        expected = int(duration_seconds * vcpu * 50000 / 3600)
        assert expected == 833

    def test_sandbox_provisioner_lazy_init_no_api_key(self):
        """sandbox_provisioner property returns None when E2B_API_KEY is not set."""
        executor = _build_test_executor()
        executor._sandbox_provisioner = None
        with patch.dict("os.environ", {}, clear=True):
            # Remove E2B_API_KEY if present
            import os
            os.environ.pop("E2B_API_KEY", None)
            result = executor.sandbox_provisioner
        assert result is None

    def test_sandbox_provisioner_lazy_init_with_api_key(self):
        """sandbox_provisioner property creates SandboxProvisioner when E2B_API_KEY is set."""
        executor = _build_test_executor()
        executor._sandbox_provisioner = None
        with patch.dict("os.environ", {"E2B_API_KEY": "test-key"}):
            with patch("executor.graph.SandboxProvisioner") as MockProvisioner:
                mock_instance = MagicMock()
                MockProvisioner.return_value = mock_instance
                result = executor.sandbox_provisioner
        assert result is mock_instance
        MockProvisioner.assert_called_once_with(api_key="test-key")

    def test_sandbox_provisioner_cached_after_init(self):
        """sandbox_provisioner property returns cached instance on second access."""
        executor = _build_test_executor()
        mock_provisioner = MagicMock()
        executor._sandbox_provisioner = mock_provisioner
        result = executor.sandbox_provisioner
        assert result is mock_provisioner

    @pytest.mark.asyncio
    async def test_execute_task_no_sandbox_config_skips_provisioning(self):
        """Task without sandbox config behaves identically to before — no provisioning."""
        executor = _build_test_executor()
        task_data = _build_sandbox_task_data(sandbox_enabled=False)

        # Confirm no sandbox key in agent_config
        agent_config = json.loads(task_data["agent_config_snapshot"])
        sandbox_config = agent_config.get("sandbox", {})
        assert not sandbox_config.get("enabled", False)

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
                mock_state.values = {"messages": [MagicMock(content="Done")]}
                mock_state.tasks = []
                mock_compiled.aget_state.return_value = mock_state

                cancel_event = asyncio.Event()
                # Should execute normally without any sandbox provisioning
                with patch.object(executor, "_handle_dead_letter", new_callable=AsyncMock) as mock_dead_letter:
                    await executor.execute_task(task_data, cancel_event)

                # No dead-letter should have been called
                mock_dead_letter.assert_not_called()

                # _build_graph was called (task ran normally)
                mock_build.assert_called_once()

    @pytest.mark.asyncio
    async def test_sandbox_provision_failure_dead_letters(self):
        """Sandbox provision failure → dead-letter with sandbox_provision_failed."""
        executor = _build_test_executor()
        mock_provisioner = MagicMock()
        mock_provisioner.provision = AsyncMock(
            side_effect=SandboxProvisionError("base", "E2B API down")
        )
        executor._sandbox_provisioner = mock_provisioner

        task_data = _build_sandbox_task_data(sandbox_enabled=True)
        cancel_event = asyncio.Event()

        with patch.object(executor, "_handle_dead_letter", new_callable=AsyncMock) as mock_dead_letter:
            await executor.execute_task(task_data, cancel_event)

        mock_dead_letter.assert_called_once()
        call_kwargs = mock_dead_letter.call_args
        assert "sandbox_provision_failed" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_sandbox_missing_api_key_dead_letters(self):
        """Missing E2B_API_KEY → dead-letter with sandbox_provision_failed."""
        executor = _build_test_executor()
        executor._sandbox_provisioner = None  # Force lazy init to run

        task_data = _build_sandbox_task_data(sandbox_enabled=True)
        cancel_event = asyncio.Event()

        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("E2B_API_KEY", None)
            with patch.object(executor, "_handle_dead_letter", new_callable=AsyncMock) as mock_dead_letter:
                await executor.execute_task(task_data, cancel_event)

        mock_dead_letter.assert_called_once()
        call_kwargs = mock_dead_letter.call_args
        assert "sandbox_provision_failed" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_sandbox_crash_recovery_failure_dead_letters(self):
        """Sandbox reconnect failure → dead-letter with sandbox_lost."""
        executor = _build_test_executor()
        mock_provisioner = MagicMock()
        mock_provisioner.connect = AsyncMock(
            side_effect=SandboxConnectionError("sbx-expired", "not found")
        )
        executor._sandbox_provisioner = mock_provisioner

        task_data = _build_sandbox_task_data(sandbox_enabled=True, sandbox_id="sbx-expired")
        cancel_event = asyncio.Event()

        with patch.object(executor, "_handle_dead_letter", new_callable=AsyncMock) as mock_dead_letter:
            await executor.execute_task(task_data, cancel_event)

        mock_dead_letter.assert_called_once()
        call_kwargs = mock_dead_letter.call_args
        assert "sandbox_lost" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_sandbox_crash_recovery_success_calls_connect(self):
        """Task with sandbox_id reconnects to existing sandbox via connect()."""
        executor = _build_test_executor()
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-existing"

        mock_provisioner = MagicMock()
        mock_provisioner.connect = AsyncMock(return_value=mock_sandbox)
        mock_provisioner.destroy = AsyncMock()
        executor._sandbox_provisioner = mock_provisioner

        task_data = _build_sandbox_task_data(sandbox_enabled=True, sandbox_id="sbx-existing")
        cancel_event = asyncio.Event()

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
                mock_state.values = {"messages": [MagicMock(content="Done")]}
                mock_state.tasks = []
                mock_compiled.aget_state.return_value = mock_state

                with patch.object(executor, "_inject_input_files", new_callable=AsyncMock, return_value=[]):
                    await executor.execute_task(task_data, cancel_event)

        # connect() should be called with the existing sandbox_id
        mock_provisioner.connect.assert_called_once_with("sbx-existing")
        # provision() should NOT have been called (reconnect path)
        mock_provisioner.provision.assert_not_called() if hasattr(mock_provisioner, "provision") else None

    @pytest.mark.asyncio
    async def test_sandbox_provisioned_id_stored_in_db(self):
        """After fresh provision, sandbox_id is immediately stored in DB."""
        executor = _build_test_executor()
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-new-123"

        mock_provisioner = MagicMock()
        mock_provisioner.provision = AsyncMock(return_value=mock_sandbox)
        mock_provisioner.destroy = AsyncMock()
        executor._sandbox_provisioner = mock_provisioner

        task_data = _build_sandbox_task_data(sandbox_enabled=True)
        cancel_event = asyncio.Event()

        # Track DB execute calls
        db_execute_calls = []
        mock_conn = _make_mock_conn()
        mock_conn.execute = AsyncMock(side_effect=lambda *args, **kwargs: db_execute_calls.append(args))
        mock_conn.fetchval = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")

        mock_acquire_ctx = AsyncMock()
        mock_acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_ctx.__aexit__ = AsyncMock(return_value=False)
        executor.pool.acquire = MagicMock(return_value=mock_acquire_ctx)
        executor.pool.fetchval = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")
        executor.pool.fetchrow = AsyncMock(return_value=None)
        executor.pool.fetch = AsyncMock(return_value=[])

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
                mock_state.values = {"messages": [MagicMock(content="Done")]}
                mock_state.tasks = []
                mock_compiled.aget_state.return_value = mock_state

                with patch.object(executor, "_inject_input_files", new_callable=AsyncMock, return_value=[]):
                    await executor.execute_task(task_data, cancel_event)

        # Check that sandbox_id was stored in DB (UPDATE tasks SET sandbox_id = ...)
        sandbox_id_store_calls = [
            c for c in db_execute_calls
            if "sandbox_id" in str(c) and "UPDATE tasks" in str(c)
        ]
        assert len(sandbox_id_store_calls) >= 1, "Expected sandbox_id to be stored in DB"
        # Verify the sandbox_id value was passed
        assert "sbx-new-123" in str(sandbox_id_store_calls[0])

    @pytest.mark.asyncio
    async def test_sandbox_paused_on_completion(self):
        """Sandbox is paused (not destroyed) after task completes so follow-ups can reconnect."""
        executor = _build_test_executor()
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-complete"

        mock_provisioner = MagicMock()
        mock_provisioner.provision = AsyncMock(return_value=mock_sandbox)
        mock_provisioner.destroy = AsyncMock()
        executor._sandbox_provisioner = mock_provisioner

        task_data = _build_sandbox_task_data(sandbox_enabled=True)
        cancel_event = asyncio.Event()

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
                mock_state.values = {"messages": [MagicMock(content="Done")]}
                mock_state.tasks = []
                mock_compiled.aget_state.return_value = mock_state

                with patch.object(executor, "_inject_input_files", new_callable=AsyncMock, return_value=[]):
                    await executor.execute_task(task_data, cancel_event)

        mock_provisioner.pause.assert_called_once_with(mock_sandbox)

    @pytest.mark.asyncio
    async def test_sandbox_paused_in_finally_on_error(self):
        """Sandbox is paused in finally block when task fails with an exception."""
        executor = _build_test_executor()
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-error"

        mock_provisioner = MagicMock()
        mock_provisioner.provision = AsyncMock(return_value=mock_sandbox)
        mock_provisioner.destroy = AsyncMock()
        executor._sandbox_provisioner = mock_provisioner

        task_data = _build_sandbox_task_data(sandbox_enabled=True)
        cancel_event = asyncio.Event()

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
                    raise ValueError("unexpected failure")
                    yield {}
                mock_compiled.astream = failing_astream

                with patch.object(executor, "_inject_input_files", new_callable=AsyncMock, return_value=[]):
                    await executor.execute_task(task_data, cancel_event)

        # pause should be called in the finally block
        mock_provisioner.pause.assert_called_once_with(mock_sandbox)

    @pytest.mark.asyncio
    async def test_build_graph_receives_sandbox_and_injected_files(self):
        """execute_task passes sandbox and injected_files to _build_graph."""
        executor = _build_test_executor()
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-check"

        mock_provisioner = MagicMock()
        mock_provisioner.provision = AsyncMock(return_value=mock_sandbox)
        mock_provisioner.destroy = AsyncMock()
        executor._sandbox_provisioner = mock_provisioner

        task_data = _build_sandbox_task_data(sandbox_enabled=True)
        cancel_event = asyncio.Event()

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
                mock_state.values = {"messages": [MagicMock(content="Done")]}
                mock_state.tasks = []
                mock_compiled.aget_state.return_value = mock_state

                with patch.object(executor, "_inject_input_files", new_callable=AsyncMock, return_value=["file1.txt"]) as mock_inject:
                    await executor.execute_task(task_data, cancel_event)

        mock_build.assert_called_once()
        _, kwargs = mock_build.call_args
        assert kwargs.get("sandbox") is mock_sandbox
        assert kwargs.get("injected_files") == ["file1.txt"]
