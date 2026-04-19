"""Graph-topology tests — Phase 2 Track 5 Task 6 + Task 12.

Verifies that :meth:`GraphExecutor._build_graph` emits the right shape based
on the :class:`MemoryDecision` passed in:

- ``skip`` / memory-disabled: no ``memory_write`` node; state schema stays
  ``MessagesState``; no save_memory tool.
- ``always`` mode: ``memory_write`` node present; direct terminal routing
  from ``agent`` resolves to ``memory_write`` (via ``route_after_agent``)
  when no tool calls are pending.
- ``agent_decides`` mode: same topology as ``always`` but the terminal
  branch resolves to ``memory_write`` ONLY when ``memory_opt_in=True``;
  otherwise resolves to ``END``.

The unit tests in this file do not exercise any real LLM — they patch
:func:`executor.providers.create_llm` and inspect the graph structure.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, MessagesState, START

from core.config import WorkerConfig
from executor.graph import GraphExecutor
from executor.memory_graph import (
    MEMORY_WRITE_NODE_NAME,
    MemoryDecision,
    MemoryEnabledState,
)


def _make_executor() -> GraphExecutor:
    config = WorkerConfig(worker_id="test-worker", tenant_id="default")
    return GraphExecutor(config, MagicMock())


async def _compile(agent_config: dict, decision: MemoryDecision | None):
    """Build and return a compiled-graph's structure for inspection."""
    executor = _make_executor()
    cancel_event = asyncio.Event()
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock()
    with patch("executor.providers.create_llm", AsyncMock(return_value=llm)):
        workflow = await executor._build_graph(
            agent_config,
            cancel_event=cancel_event,
            task_id="t",
            tenant_id="default",
            agent_id="a",
            memory_decision=decision,
            task_input="some task input",
        )
    return workflow


class TestBuildGraphSkipMode:
    """`memory_mode='skip'` (or memory.enabled=False) — stack disabled."""

    @pytest.mark.asyncio
    async def test_no_memory_node_and_messages_state(self) -> None:
        workflow = await _compile(
            {"model": "claude-haiku-4-5", "allowed_tools": []},
            MemoryDecision(stack_enabled=False, auto_write=False),
        )

        # State schema is MessagesState — NOT MemoryEnabledState.
        assert workflow.state_schema is MessagesState

        # No memory_write node exists.
        node_names = set(workflow.nodes.keys())
        assert MEMORY_WRITE_NODE_NAME not in node_names
        # Only the agent node (tools absent because allowed_tools is empty).
        assert "agent" in node_names


class TestBuildGraphAlwaysMode:
    """`memory_mode='always'` — stack on, auto-write on."""

    @pytest.mark.asyncio
    async def test_memory_node_present_and_memory_enabled_state(self) -> None:
        workflow = await _compile(
            {
                "model": "claude-haiku-4-5",
                "allowed_tools": [],
                "memory": {"enabled": True},
            },
            MemoryDecision(stack_enabled=True, auto_write=True),
        )

        assert workflow.state_schema is MemoryEnabledState

        node_names = set(workflow.nodes.keys())
        assert MEMORY_WRITE_NODE_NAME in node_names
        assert "agent" in node_names


class TestBuildGraphAgentDecidesMode:
    """`memory_mode='agent_decides'` — stack on, auto-write off.

    Topology is identical to ``always`` (same node set, same state schema)
    so the terminal branch exists for the ``save_memory`` opt-in runtime
    check. The per-branch resolution is covered by the route function
    tests below.
    """

    @pytest.mark.asyncio
    async def test_same_topology_as_always_mode(self) -> None:
        workflow = await _compile(
            {
                "model": "claude-haiku-4-5",
                "allowed_tools": [],
                "memory": {"enabled": True},
            },
            MemoryDecision(stack_enabled=True, auto_write=False),
        )

        assert workflow.state_schema is MemoryEnabledState
        node_names = set(workflow.nodes.keys())
        assert MEMORY_WRITE_NODE_NAME in node_names
        assert "agent" in node_names


class TestRouteAfterAgent:
    """The ``route_after_agent`` closure is the single decision point out
    of the ``agent`` node. Exercising it directly — without compiling —
    lets us assert every branch independently of LangGraph's internals.

    We build a small throwaway ``_build_graph`` invocation, locate the
    registered conditional edge, and evaluate it against a few crafted
    states.
    """

    @pytest.mark.asyncio
    async def test_always_mode_routes_terminal_to_memory_write(self) -> None:
        workflow = await _compile(
            {
                "model": "claude-haiku-4-5",
                "allowed_tools": [],
                "memory": {"enabled": True},
            },
            MemoryDecision(stack_enabled=True, auto_write=True),
        )
        # LangGraph stores the conditional edges in ``branches``.
        agent_branches = workflow.branches.get("agent") or {}
        assert agent_branches, (
            "agent node should expose a conditional edge "
            "(route_after_agent) when the memory stack is enabled"
        )
        # Pull the first conditional edge's path function and evaluate it.
        branch = next(iter(agent_branches.values()))

        def path(state):
            return branch.path.invoke(state)
        # No pending tool calls, auto_write=True → memory_write.
        state = {"messages": [AIMessage(content="done")], "memory_opt_in": False}
        assert path(state) == MEMORY_WRITE_NODE_NAME

    @pytest.mark.asyncio
    async def test_agent_decides_no_opt_in_routes_to_end(self) -> None:
        workflow = await _compile(
            {
                "model": "claude-haiku-4-5",
                "allowed_tools": [],
                "memory": {"enabled": True},
            },
            MemoryDecision(stack_enabled=True, auto_write=False),
        )
        agent_branches = workflow.branches.get("agent") or {}
        branch = next(iter(agent_branches.values()))

        def path(state):
            return branch.path.invoke(state)
        # No opt-in → END (silent no-op).
        state = {"messages": [AIMessage(content="done")], "memory_opt_in": False}
        assert path(state) == END

    @pytest.mark.asyncio
    async def test_agent_decides_with_opt_in_routes_to_memory_write(self) -> None:
        workflow = await _compile(
            {
                "model": "claude-haiku-4-5",
                "allowed_tools": [],
                "memory": {"enabled": True},
            },
            MemoryDecision(stack_enabled=True, auto_write=False),
        )
        agent_branches = workflow.branches.get("agent") or {}
        branch = next(iter(agent_branches.values()))

        def path(state):
            return branch.path.invoke(state)
        # Opt-in True → memory_write.
        state = {"messages": [AIMessage(content="done")], "memory_opt_in": True}
        assert path(state) == MEMORY_WRITE_NODE_NAME

    @pytest.mark.asyncio
    async def test_pending_tool_calls_always_route_to_tools_branch(
        self,
    ) -> None:
        """With a registered tool the "tools" branch must win over
        memory/END regardless of opt-in or auto_write — otherwise we'd drop
        pending tool calls on the terminal super-step.
        """
        from langchain_core.tools import StructuredTool
        from pydantic import BaseModel, Field

        class _NoArgs(BaseModel):
            dummy: str = Field(default="x")

        def _noop(dummy: str = "x") -> str:
            return "ok"

        # Provide at least one custom tool so ``tools`` node is registered.
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
                memory_decision=MemoryDecision(
                    stack_enabled=True, auto_write=False
                ),
                task_input="some task input",
                custom_tools=[
                    StructuredTool.from_function(
                        func=_noop,
                        name="noop",
                        description="test tool",
                        args_schema=_NoArgs,
                    )
                ],
            )

        agent_branches = workflow.branches.get("agent") or {}
        branch = next(iter(agent_branches.values()))

        def path(state):
            return branch.path.invoke(state)

        # A message with pending tool calls must route to "tools" even with
        # opt-in set.
        ai = AIMessage(content="")
        ai.tool_calls = [{"id": "1", "name": "noop", "args": {}}]
        state = {"messages": [ai], "memory_opt_in": True}
        assert path(state) == "tools"
