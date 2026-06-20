"""S3 — ONE Postgres-backed confirmation of the sub-agent durability contract.

The fake-model unit tests in
``services/worker-service/tests/test_subagent_fanout.py`` run on an in-process
``MemorySaver``. This single integration test confirms — against the repo's real
``PostgresDurableCheckpointer`` — the two load-bearing claims the spikes proved
on ``MemorySaver`` (the spec says *verify, don't assume* the same
``pending_writes`` / namespace behaviour on real Postgres):

1. **Per-inner-turn crash resume.** A worker crash mid-sub-agent resumes the
   parent's run at the inner turn the sub-agent died on — earlier inner turns
   are restored (NOT recomputed, tokens NOT re-spent), and a completed sibling
   sub-agent is NOT recomputed.
2. **Namespaced transcript persistence.** After the run, each sub-agent's full
   turn-by-turn transcript is readable from its ``subagent:<id>`` sub-checkpoint
   namespace (the E5 / Console drill-in read path) — no new table or store.

It drives the **production** ``build_subagent_node`` (the same compiled ReAct
subgraph the helper builds) wired as a ``Send``-reached node sharing the
``PostgresDurableCheckpointer``, with ``durability="sync"``. Run via the
isolated harness: ``make e2e-test PYTEST_ARGS='-k subagent_fanout_durability'``.

Worktree-concurrency-safe: binds no ports, spawns no subprocess.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, TypedDict

import asyncpg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Send

from checkpointer.postgres import PostgresDurableCheckpointer
from executor.subagents.fanout import (
    SubagentCeiling,
    _append,
    _max_reducer,
    build_subagent_node,
)

WORKER_ID = "worker-subagent-durability"
TENANT_ID = "default"

# Module-level crash sentinel: the model crashes the ``alpha`` branch at its
# 2nd turn on the FIRST run, then succeeds on resume (transient fault gone).
_CRASH_ARMED = {"alpha_turn2": True}


class _CrashingModel:
    """Deterministic model: emit a tool call on turn 1, then a final answer on
    turn 2 — but crash the ``alpha`` branch's turn-2 call once."""

    def __init__(self) -> None:
        self.bound = None

    def bind_tools(self, tools, **kwargs):
        self.bound = list(tools)
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        # The seed HumanMessage carries the subtask label.
        first_human = next(
            (m for m in messages if isinstance(m, HumanMessage)), None
        )
        subtask = (first_human.content if first_human else "") or ""
        turn = sum(1 for m in messages if isinstance(m, AIMessage)) + 1
        um = {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}
        if turn == 1:
            return AIMessage(
                id=f"{subtask}-ai-1",
                content="",
                tool_calls=[{"name": "note", "args": {"text": subtask}, "id": f"{subtask}-c1"}],
                usage_metadata=um,
            )
        # turn 2 — the final answer, but crash alpha once.
        if subtask == "alpha" and _CRASH_ARMED["alpha_turn2"]:
            _CRASH_ARMED["alpha_turn2"] = False
            raise RuntimeError("SIMULATED crash mid-sub-agent (alpha, inner turn 2)")
        return AIMessage(
            id=f"{subtask}-ai-2",
            content=f"FINDING({subtask})",
            usage_metadata=um,
        )


def _note_tool() -> StructuredTool:
    async def note(text: str) -> str:
        return f"noted:{text}"

    return StructuredTool.from_function(coroutine=note, name="note", description="note")


class _Parent(TypedDict, total=False):
    subtasks: list
    results: Annotated[list, _append]
    # mirror the sub-agent counters so they map back for assertions if needed
    turn_count: Annotated[int, _max_reducer]


async def _emit(*_args, **_kwargs) -> None:
    return None


async def _insert_running_task(pool: asyncpg.Pool, task_id: str) -> None:
    agent_config = json.dumps({
        "system_prompt": "test",
        "model": "claude-sonnet-4-6",
        "temperature": 0.1,
        "allowed_tools": ["note"],
    })
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
            VALUES ($1, 'subagent_agent', 'Subagent Agent', $2::jsonb, 'active')
            ON CONFLICT (tenant_id, agent_id) DO NOTHING
            """,
            TENANT_ID, agent_config,
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                task_id, tenant_id, agent_id, agent_config_snapshot,
                status, input, lease_owner, lease_expiry, version
            ) VALUES (
                $1::uuid, $2, 'subagent_agent', $3::jsonb,
                'running', 'input', $4, NOW() + INTERVAL '300 seconds', 1
            )
            """,
            task_id, TENANT_ID, agent_config, WORKER_ID,
        )


def _build_parent(checkpointer: PostgresDurableCheckpointer):
    model = _CrashingModel()
    sub = build_subagent_node(
        model=model,
        tools=[_note_tool()],
        ceiling=SubagentCeiling(max_turns=8, max_tokens=10_000_000),
        emit=_emit,
    )

    def planner(_state):
        return {"subtasks": ["alpha", "beta"]}

    def route(state):
        return [
            Send("subagent", {
                "sub_messages": [HumanMessage(content=t)],
                "turn_count": 0, "tokens_used": 0, "depth": 1,
            })
            for t in state["subtasks"]
        ]

    g = StateGraph(_Parent)
    g.add_node("planner", planner)
    g.add_node("subagent", sub)
    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", route, ["subagent"])
    g.add_edge("subagent", END)
    return g.compile(checkpointer=checkpointer)


@pytest.mark.asyncio
async def test_subagent_fanout_durability_on_postgres(db_pool: asyncpg.Pool) -> None:
    _CRASH_ARMED["alpha_turn2"] = True
    task_id = str(uuid.uuid4())
    await _insert_running_task(db_pool, task_id)

    checkpointer = PostgresDurableCheckpointer(
        db_pool, worker_id=WORKER_ID, tenant_id=TENANT_ID
    )
    cfg = {"configurable": {"thread_id": task_id}}

    # ----- run 1: crashes mid-sub-agent (alpha, inner turn 2) -----
    graph = _build_parent(checkpointer)
    with pytest.raises(RuntimeError, match="SIMULATED crash"):
        await graph.ainvoke({}, config=cfg, durability="sync")

    # ----- run 2: resume from Postgres (fresh graph + model instance) -----
    graph2 = _build_parent(checkpointer)
    final = await graph2.ainvoke(None, config=cfg, durability="sync")

    # Both findings present; the run completed.
    summaries = sorted(
        r.get("summary", "") for r in (final.get("results") or [])
    )
    assert summaries == ["FINDING(alpha)", "FINDING(beta)"], summaries

    # ----- transcript persistence: read each sub-agent's namespaced checkpoint
    found_namespaces: dict[str, list[str]] = {}
    async for ct in checkpointer.alist(cfg):
        ns = ct.config["configurable"].get("checkpoint_ns", "")
        if not ns.startswith("subagent:"):
            continue
        cv = ct.checkpoint.get("channel_values", {})
        sub_msgs = cv.get("sub_messages")
        if sub_msgs:
            texts = [getattr(m, "content", "") for m in sub_msgs]
            # keep the richest (longest) snapshot per namespace
            if len(texts) >= len(found_namespaces.get(ns, [])):
                found_namespaces[ns] = texts

    # Two sub-agent namespaces persisted, each with a full transcript.
    assert len(found_namespaces) == 2, found_namespaces
    all_text = " ".join(t for texts in found_namespaces.values() for t in texts)
    assert "FINDING(alpha)" in all_text
    assert "FINDING(beta)" in all_text


@pytest.mark.asyncio
async def test_subagent_per_inner_turn_resume_does_not_recompute(
    db_pool: asyncpg.Pool,
) -> None:
    """The crashed branch resumes at its failing inner turn — turn 1 is NOT
    recomputed — and the completed sibling branch is NOT recomputed.

    We assert this via the turn-1 tool result persisted in the alpha
    sub-checkpoint surviving the crash/resume cycle exactly once, and the
    completed run reaching both findings."""
    _CRASH_ARMED["alpha_turn2"] = True
    task_id = str(uuid.uuid4())
    await _insert_running_task(db_pool, task_id)
    checkpointer = PostgresDurableCheckpointer(
        db_pool, worker_id=WORKER_ID, tenant_id=TENANT_ID
    )
    cfg = {"configurable": {"thread_id": task_id}}

    graph = _build_parent(checkpointer)
    with pytest.raises(RuntimeError, match="SIMULATED crash"):
        await graph.ainvoke({}, config=cfg, durability="sync")

    # After the crash, alpha's turn-1 work (the tool call + tool result) is
    # already checkpointed under its namespace — proving the inner super-step
    # persisted before the turn-2 crash.
    alpha_turn1_persisted = False
    async for ct in checkpointer.alist(cfg):
        ns = ct.config["configurable"].get("checkpoint_ns", "")
        if not ns.startswith("subagent:"):
            continue
        cv = ct.checkpoint.get("channel_values", {})
        for m in cv.get("sub_messages") or []:
            if "noted:alpha" in (getattr(m, "content", "") or ""):
                alpha_turn1_persisted = True
    assert alpha_turn1_persisted, "alpha turn-1 inner super-step was not checkpointed pre-crash"

    # Resume completes without recomputing turn 1 (the transient fault is gone).
    graph2 = _build_parent(checkpointer)
    final = await graph2.ainvoke(None, config=cfg, durability="sync")
    summaries = sorted(r.get("summary", "") for r in (final.get("results") or []))
    assert summaries == ["FINDING(alpha)", "FINDING(beta)"], summaries
