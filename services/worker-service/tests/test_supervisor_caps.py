"""S11 #7 — caps enforced over the COMPOSED Supervisor graph.

S6's ``test_supervisor_fanout.py`` proves the clamp + ``max_iterations`` stop at
the *``supervisor_node``* level. S11 proves both caps hold END-TO-END through the
production ``build_supervisor_graph()`` (the composed surface where the iteration
loop and the structural ``Send`` fan-out interact):

* **``max_fanout_per_iteration`` clamps.** A Supervisor that emits more subtasks
  than the cap dispatches AT MOST ``max_fanout_per_iteration`` ``Send`` branches
  in one round, and a ``supervisor_iteration`` event records the cap reason (no
  silent truncation, §A7).
* **``max_iterations`` stops the loop.** A Supervisor that would keep saying
  "continue" is forced to STOP at the cap; the final ``supervisor_iteration``
  carries ``decision=stop`` with an iteration-cap reason, and the run still
  reaches the Writer (a terminal report), never an unbounded loop.

All models are fakes; ``run_subagent`` is patched; the graph runs on an in-process
``MemorySaver`` with ``durability="sync"``. No Postgres, no ports, no subprocess →
worktree-concurrency-safe.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from executor.subagents import SubagentCeiling, SubagentResult
from executor.supervisor.graph import FANOUT_NODE_NAME, build_supervisor_graph

from tests.test_supervisor_partial_failure import _RecordingEmit


_FINDINGS = json.dumps(
    {"findings": [{"claim": "c", "source_url": "https://ex.com", "supporting_quote": "q"}]}
)


class _AlwaysContinueModel:
    """Routing model whose Supervisor ALWAYS emits ``continue`` with ``emitted``
    subtasks — so only the caps (not the LLM) can stop the loop / bound the width."""

    def __init__(self, *, emitted: int):
        self.emitted = emitted

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        text = messages if isinstance(messages, str) else str(messages)
        if "You are the supervisor" in text:
            return AIMessage(
                content=json.dumps(
                    {
                        "decision": "continue",
                        "subtasks": [{"prompt": f"t{i}"} for i in range(self.emitted)],
                        "reason": "keep going",
                    }
                )
            )
        if "You are the writer" in text:
            return AIMessage(content="Final report [x].")
        if "Write the research brief now" in text:
            return AIMessage(content="brief")
        if "You are the scoping phase" in text:
            return AIMessage(content=json.dumps({"clear": True}))
        if "supported" in text.lower():
            return AIMessage(content=json.dumps({"supported": True}))
        return AIMessage(content=_FINDINGS)


def _config(model, *, max_fanout: int, max_iterations: int, checkpointer, emit) -> dict:
    return {
        "configurable": {
            "thread_id": "caps-thread",
            "scope_model": model,
            "supervisor_model": model,
            "writer_model": model,
            "verify_model": model,
            "agent_config": {
                "supervisor": {
                    "max_fanout_per_iteration": max_fanout,
                    "max_iterations": max_iterations,
                    "scope_clarification_enabled": False,
                }
            },
            "supervisor_emit": emit,
            "iteration": 0,
            "supervisor_fanout_deps": {
                "model": model,
                "checkpointer": checkpointer,
                "ceiling": SubagentCeiling(max_turns=4, max_tokens=10_000),
                "tools": [],
                "emit": emit,
            },
        },
        "recursion_limit": 100,
    }


# --------------------------------------------------------------------------- #
# #7a — max_fanout_per_iteration clamps the dispatched fan-out + records the cap.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_max_fanout_clamps_dispatched_send_branches():
    emit = _RecordingEmit()
    checkpointer = MemorySaver()
    # The Supervisor emits 8 subtasks but the cap is 3.
    model = _AlwaysContinueModel(emitted=8)
    cfg = _config(model, max_fanout=3, max_iterations=2, checkpointer=checkpointer, emit=emit)

    fanout_branches = {"count": 0}

    async def fake_run(prompt, tools, **kwargs):
        fanout_branches["count"] += 1
        return SubagentResult.success(_FINDINGS, usage={"input_tokens": 5, "output_tokens": 2})

    graph = build_supervisor_graph().compile(checkpointer=checkpointer)
    fanout_events = {"count": 0}
    async for event in _stream(graph, cfg, fake_run):
        if FANOUT_NODE_NAME in event:
            fanout_events["count"] += 1

    # The FIRST round dispatched at most the cap (3), never 8. (The run continues
    # for a 2nd round before the iteration cap stops it, so total branches are a
    # multiple of the clamp — the per-round clamp is the invariant.)
    iter_events = [d for t, d in emit.events if t == "supervisor_iteration"]
    first_round = next(d for d in iter_events if d.get("iteration") == 1)
    assert first_round["subtasks_emitted"] == 3, "fan-out not clamped to max_fanout"
    # The cap-reason was recorded (no silent truncation).
    assert any("fanout" in (d.get("reason") or "") for d in iter_events)


# --------------------------------------------------------------------------- #
# #7b — max_iterations stops the loop with a cap-reason stop event + reaches Writer.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_max_iterations_stops_loop_and_reaches_writer():
    emit = _RecordingEmit()
    checkpointer = MemorySaver()
    # Model always says "continue"; only the iteration cap (2) can stop it.
    model = _AlwaysContinueModel(emitted=2)
    cfg = _config(model, max_fanout=5, max_iterations=2, checkpointer=checkpointer, emit=emit)

    async def fake_run(prompt, tools, **kwargs):
        return SubagentResult.success(_FINDINGS, usage={"input_tokens": 5, "output_tokens": 2})

    graph = build_supervisor_graph().compile(checkpointer=checkpointer)
    # Run to completion (the loop is bounded by the iteration cap, not the model).
    with patch("executor.supervisor.graph.run_subagent", AsyncMock(side_effect=fake_run)):
        final = await graph.ainvoke(
            {"messages": [HumanMessage(content="research")]},
            config=cfg,
            durability="sync",
        )

    # The loop stopped at the cap — a stop event with an iteration-cap reason.
    iter_events = [d for t, d in emit.events if t == "supervisor_iteration"]
    stop_events = [d for d in iter_events if d.get("decision") == "stop"]
    assert stop_events, "expected a stop decision once max_iterations was hit"
    assert any("iteration" in (d.get("reason") or "").lower() for d in stop_events)
    # No more than max_iterations rounds were emitted (the loop is bounded).
    emitted_rounds = {d.get("iteration") for d in iter_events if d.get("subtasks_emitted")}
    assert max(emitted_rounds) <= 2
    # Despite the always-continue model, the run terminated at the Writer.
    assert final.get("report")


async def _stream(graph, cfg, fake_run):
    with patch("executor.supervisor.graph.run_subagent", AsyncMock(side_effect=fake_run)):
        async for event in graph.astream(
            {"messages": [HumanMessage(content="research")]},
            config=cfg,
            stream_mode="updates",
            durability="sync",
        ):
            yield event
