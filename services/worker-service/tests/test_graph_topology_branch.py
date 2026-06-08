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
from executor.graph import GraphExecutor
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
