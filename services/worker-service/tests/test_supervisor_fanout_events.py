"""S11 #1 — fan-out determinism at the OBSERVABLE-event level over the composed graph.

S6's ``test_supervisor_fanout.py::test_send_fanout_dispatches_n_subagents`` proves
the *structural* determinism (N subtasks → N ``Send`` branches → N distinct
``subtask`` keys in ``subagent_results``, in one super-step). This test adds the
behavior #1 *observable* the spec names: the production ``build_supervisor_graph()``
emits exactly N ``subagent_started`` events with DISTINCT ``subtask`` ids under the
SAME ``iteration`` round — structural (deterministic), not LLM-emergent.

Fake models; patched ``run_subagent``; in-process ``MemorySaver`` with
``durability="sync"``. No Postgres, no ports, no subprocess → worktree-safe.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from executor.subagents import SubagentCeiling, SubagentResult
from executor.supervisor.graph import build_supervisor_graph

from tests.test_supervisor_partial_failure import _RecordingEmit, _RoutingModel


_FINDINGS = json.dumps(
    {"findings": [{"claim": "c", "source_url": "https://ex.com", "supporting_quote": "q"}]}
)


@pytest.mark.asyncio
async def test_fanout_emits_n_distinct_subagent_started_under_one_iteration():
    n = 4
    emit = _RecordingEmit()
    checkpointer = MemorySaver()
    model = _RoutingModel(n_subtasks=n)
    cfg = {
        "configurable": {
            "thread_id": "fanout-events-thread",
            "scope_model": model,
            "supervisor_model": model,
            "writer_model": model,
            "verify_model": model,
            "agent_config": {
                "supervisor": {
                    "max_fanout_per_iteration": 10,
                    "max_iterations": 3,
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
        "recursion_limit": 50,
    }

    async def fake_run(prompt, tools, **kwargs):
        return SubagentResult.success(_FINDINGS, usage={"input_tokens": 5, "output_tokens": 2})

    graph = build_supervisor_graph().compile(checkpointer=checkpointer)
    with patch(
        "executor.supervisor.graph.run_subagent",
        AsyncMock(side_effect=fake_run),
    ):
        out = await graph.ainvoke(
            {"messages": [HumanMessage(content="research")]},
            config=cfg,
            durability="sync",
        )

    # The first round emitted exactly N subagent_started events with DISTINCT
    # subtask ids — deterministic structural fan-out. We key the round off the
    # subtask id prefix (the reliable round signal: ids are minted as
    # f"{iteration}.{index}" by S6, §A11-E8), NOT the event's ``iteration`` field.
    #
    # KNOWN DEFECT (reported as a follow-up, NOT fixed in S11): the ``iteration``
    # field carried on marker.subagent.* events is statically 0 for EVERY round,
    # because ``_fanout_node`` reads it from ``config['configurable']['iteration']``
    # (injected once as 0 by ``_inject_supervisor_configurable`` and never advanced
    # per round) instead of from the per-round state. S10's Console tree groups by
    # this field (``buildSubagentTree``: ``item.event.iteration ?? 0``), so all
    # sub-agents from all rounds collapse into "Round 0" while the
    # supervisor_iteration markers carry the real round — a grouping mismatch.
    # Owning tasks: S8 (inject/advance iteration) + S9 (event payload) + S10
    # (Console grouping). Tracked separately from S11's verification scope.
    started = [d for t, d in emit.events if t == "subagent_started"]
    first_round = [d for d in started if str(d["subtask"]).startswith("1.")]
    assert len(first_round) == n
    subtasks = {d["subtask"] for d in first_round}
    assert len(subtasks) == n, "subtask ids collided — fan-out determinism broken"
    # The minted ids are the deterministic f"{iteration}.{index}" form (§A11-E8).
    assert subtasks == {f"1.{i}" for i in range(n)}
    assert out.get("report")
