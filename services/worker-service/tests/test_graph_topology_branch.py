"""`_build_graph` topology branch selection (Task S8, §A5/§A11).

Fake-model, no-infra unit tests over ``GraphExecutor._build_graph``:

* ``topology`` absent / ``"react"`` → the existing ReAct graph (``agent`` node);
* ``topology = "supervisor"`` → the Supervisor graph (scope → supervisor ⇄
  fan-out → writer), NO ReAct ``agent`` node;
* any other ``topology`` value → a build-time error (defensive, NOT a silent
  ReAct fallback).

Worktree-safe: binds no ports, spawns no subprocess.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import WorkerConfig
from executor.graph import GraphExecutor, _filter_subagent_source_tools
from executor.supervisor.graph import (
    GATHER_NODE_NAME,
    FANOUT_NODE_NAME,
    SUPERVISOR_NODE_NAME,
)


def _make_executor() -> GraphExecutor:
    return GraphExecutor(WorkerConfig(worker_id="test-worker", tenant_id="default"), MagicMock())


async def _build(agent_config: dict):
    executor = _make_executor()
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock()
    with patch("executor.providers.create_llm", AsyncMock(return_value=llm)):
        return await executor._build_graph(
            agent_config,
            cancel_event=asyncio.Event(),
            task_id="t",
            tenant_id="default",
            agent_id="a",
            task_input="research X",
        )


async def test_absent_topology_builds_react_graph():
    workflow = await _build({"model": "claude-haiku-4-5", "allowed_tools": []})
    nodes = set(workflow.nodes.keys())
    assert "agent" in nodes
    assert SUPERVISOR_NODE_NAME not in nodes


async def test_react_topology_builds_react_graph():
    workflow = await _build(
        {"model": "claude-haiku-4-5", "allowed_tools": [], "topology": "react"}
    )
    nodes = set(workflow.nodes.keys())
    assert "agent" in nodes
    assert SUPERVISOR_NODE_NAME not in nodes


async def test_supervisor_topology_builds_supervisor_graph():
    workflow = await _build(
        {
            "model": "claude-haiku-4-5",
            "allowed_tools": ["web_search"],
            "topology": "supervisor",
            "supervisor": {"max_fanout_per_iteration": 5},
        }
    )
    nodes = set(workflow.nodes.keys())
    # Supervisor topology nodes present; NO ReAct "agent" node (E1 guardrail:
    # the cost loop must work for the real supervisor node keys, not "agent").
    assert {"scope", SUPERVISOR_NODE_NAME, FANOUT_NODE_NAME, GATHER_NODE_NAME, "writer"} <= nodes
    assert "agent" not in nodes


async def test_unknown_topology_fails_build():
    with pytest.raises(ValueError):
        await _build(
            {"model": "claude-haiku-4-5", "topology": "mesh", "allowed_tools": []}
        )


async def test_supervisor_subagent_ceiling_is_research_sized():
    """Supervisor fan-out sub-agents get the platform MAX turn budget.

    Regression for task 954b2811: with the 8-turn dispatch default, all 3
    research sub-agents exhausted their ceiling mid-search (paywalled /
    dead URLs burn turns) and returned zero findings, so round 1 all-failed
    and the task dead-lettered. Deep-research sub-tasks need the platform
    max; cost stays bounded by ``max_fanout × ceiling`` (§A5/§A8/D3).
    """
    from tools.subagent_tools import MAX_SUBAGENT_TURN_BUDGET

    executor = _make_executor()
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    config: dict = {"configurable": {}}
    with patch("executor.providers.create_llm", AsyncMock(return_value=llm)):
        await executor._inject_supervisor_configurable(
            config,
            agent_config={
                "model": "claude-haiku-4-5",
                "allowed_tools": ["web_search"],
                "topology": "supervisor",
            },
            checkpointer=MagicMock(),
            task_id="t",
            tenant_id="default",
            agent_id="a",
            cancel_event=asyncio.Event(),
        )
    ceiling = config["configurable"]["supervisor_fanout_deps"]["ceiling"]
    assert ceiling.max_turns == MAX_SUBAGENT_TURN_BUDGET


# ---------------------------------------------------------------------------
# source_allowlist enforcement (pure helper) — Codex review P2.
# ---------------------------------------------------------------------------


def test_source_allowlist_none_or_empty_is_all_sources():
    """None / empty allowlist = "all available sources": no restriction."""
    tools = ["web_search", "read_url", "plan_write"]
    assert _filter_subagent_source_tools(tools, None) == tools
    assert _filter_subagent_source_tools(tools, []) == tools


def test_source_allowlist_drops_unchecked_web_source_tool():
    """A non-empty allowlist drops web source tools not named in it (the Codex
    example: only web_search selected → read_url is removed)."""
    result = _filter_subagent_source_tools(
        ["web_search", "read_url", "plan_write"], ["web_search"]
    )
    assert result == ["web_search", "plan_write"]
    assert "read_url" not in result


def test_source_allowlist_never_filters_base_platform_tools():
    """The allowlist governs SOURCE tools only — base platform tools the sub-agent
    needs always pass even when not listed."""
    result = _filter_subagent_source_tools(
        ["web_search", "plan_write", "create_text_artifact"], ["web_search"]
    )
    assert "plan_write" in result
    assert "create_text_artifact" in result


async def test_supervisor_source_allowlist_filters_sub_tools():
    """End-to-end through _inject: source_allowlist restricts the sub-agents'
    _get_tools input to the allowed web sources (read_url dropped)."""
    executor = _make_executor()
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    config: dict = {"configurable": {}}
    with patch("executor.providers.create_llm", AsyncMock(return_value=llm)), \
            patch.object(executor, "_get_tools", MagicMock(return_value=[])) as get_tools:
        await executor._inject_supervisor_configurable(
            config,
            agent_config={
                "model": "claude-haiku-4-5",
                "allowed_tools": ["web_search", "read_url", "plan_write"],
                "topology": "supervisor",
                "supervisor": {"source_allowlist": ["web_search"]},
            },
            checkpointer=MagicMock(),
            task_id="t",
            tenant_id="default",
            agent_id="a",
            cancel_event=asyncio.Event(),
        )
    passed_allowed_tools = get_tools.call_args.args[0]
    assert passed_allowed_tools == ["web_search", "plan_write"]


async def test_supervisor_threads_real_cancel_event_into_sub_tools():
    """The sub-agent tools are built with the task's REAL cancel_event (not a
    throwaway), so an in-flight sub-agent tool call aborts on cancellation."""
    executor = _make_executor()
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    config: dict = {"configurable": {}}
    sentinel = asyncio.Event()
    with patch("executor.providers.create_llm", AsyncMock(return_value=llm)), \
            patch.object(executor, "_get_tools", MagicMock(return_value=[])) as get_tools:
        await executor._inject_supervisor_configurable(
            config,
            agent_config={
                "model": "claude-haiku-4-5",
                "allowed_tools": ["web_search"],
                "topology": "supervisor",
            },
            checkpointer=MagicMock(),
            task_id="t",
            tenant_id="default",
            agent_id="a",
            cancel_event=sentinel,
        )
    assert get_tools.call_args.kwargs["cancel_event"] is sentinel
