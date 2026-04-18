"""Graph-topology tests — Phase 2 Track 5 Task 6.

Verifies that :meth:`GraphExecutor._build_graph` emits the right shape based
on the ``memory_enabled`` flag:

- Memory-enabled: ``memory_write`` node is present on the "no pending tool
  calls" branch; ``MemoryEnabledState`` is the compiled-in state schema.
- Memory-disabled: no ``memory_write`` node; state schema stays ``MessagesState``.

The unit tests in this file do not exercise any real LLM — they patch
:func:`executor.providers.create_llm` and inspect the graph structure.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.graph import END, MessagesState, START

from core.config import WorkerConfig
from executor.graph import GraphExecutor
from executor.memory_graph import MEMORY_WRITE_NODE_NAME, MemoryEnabledState


def _make_executor() -> GraphExecutor:
    config = WorkerConfig(worker_id="test-worker", tenant_id="default")
    return GraphExecutor(config, MagicMock())


class TestBuildGraphMemoryDisabled:
    @pytest.mark.asyncio
    async def test_no_memory_node_and_messages_state(self) -> None:
        executor = _make_executor()
        cancel_event = asyncio.Event()

        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        llm.ainvoke = AsyncMock()

        with patch("executor.providers.create_llm", AsyncMock(return_value=llm)):
            workflow = await executor._build_graph(
                {"model": "claude-haiku-4-5", "allowed_tools": []},
                cancel_event=cancel_event,
                task_id="t",
                tenant_id="default",
                agent_id="a",
                memory_enabled=False,
            )

        # State schema is MessagesState — NOT MemoryEnabledState.
        assert workflow.state_schema is MessagesState

        # No memory_write node exists.
        node_names = set(workflow.nodes.keys())
        assert MEMORY_WRITE_NODE_NAME not in node_names
        # Only the agent node (tools absent because allowed_tools is empty).
        assert "agent" in node_names


class TestBuildGraphMemoryEnabled:
    @pytest.mark.asyncio
    async def test_memory_node_present_and_memory_enabled_state(self) -> None:
        executor = _make_executor()
        cancel_event = asyncio.Event()

        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        llm.ainvoke = AsyncMock()

        with patch("executor.providers.create_llm", AsyncMock(return_value=llm)):
            workflow = await executor._build_graph(
                {
                    "model": "claude-haiku-4-5",
                    "allowed_tools": [],
                    "memory": {"enabled": True},
                },
                cancel_event=cancel_event,
                task_id="t",
                tenant_id="default",
                agent_id="a",
                memory_enabled=True,
                task_input="some task input",
            )

        assert workflow.state_schema is MemoryEnabledState

        node_names = set(workflow.nodes.keys())
        assert MEMORY_WRITE_NODE_NAME in node_names
        assert "agent" in node_names
