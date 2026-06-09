"""S11 #1 — fan-out determinism at the OBSERVABLE-event level over the composed graph.

S6's ``test_supervisor_fanout.py::test_send_fanout_dispatches_n_subagents`` proves
the *structural* determinism (N subtasks → N ``Send`` branches → N distinct
``subtask`` keys in ``subagent_results``, in one super-step). This test adds the
behavior #1 *observable* the spec names: the production ``build_supervisor_graph()``
emits exactly N ``subagent_started`` events with DISTINCT ``subtask`` ids under the
SAME ``iteration`` round — structural (deterministic), not LLM-emergent.

It also pins the fix for the iteration-grouping defect S11 first surfaced: the
``marker.subagent.*`` events now carry the **live current round** (round 1 → 1,
round 2 → 2), matching S6's 1-based ``iteration`` reducer, so S10's Console
``buildSubagentTree`` (which groups on ``event.iteration``) renders one node per
round instead of collapsing every round into "Round 0". A re-dispatched
(failed-then-retried) subtask keeps its stable round-1 id while its round-2 marker
carries iteration=2 — exactly the round/linkage split S10's retry view needs.

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
    # subtask ids — deterministic structural fan-out. Both signals agree now: the
    # minted ids are f"{iteration}.{index}" (§A11-E8) AND the event's ``iteration``
    # field carries the live round (the fix — see the two-round test below).
    started = [d for t, d in emit.events if t == "subagent_started"]
    first_round = [d for d in started if str(d["subtask"]).startswith("1.")]
    assert len(first_round) == n
    subtasks = {d["subtask"] for d in first_round}
    assert len(subtasks) == n, "subtask ids collided — fan-out determinism broken"
    # The minted ids are the deterministic f"{iteration}.{index}" form (§A11-E8).
    assert subtasks == {f"1.{i}" for i in range(n)}
    # The marker carries the live round, not the static config 0.
    assert all(d["iteration"] == 1 for d in first_round)
    assert out.get("report")


# --------------------------------------------------------------------------- #
# Iteration-grouping fix — markers carry the DISTINCT live round per round, and
# a re-dispatched subtask keeps its round-1 id while its round-2 marker is round 2.
# --------------------------------------------------------------------------- #


class _TwoRoundRedispatchModel:
    """Supervisor: round 1 → continue (2 subtasks); round 2 → continue,
    re-dispatching the failed round-1 subtask ``1.1`` (carry-forward id); round 3
    → stop. Scope/writer/verify routed like ``_RoutingModel``.

    The re-dispatch only carries the id forward if ``1.1`` holds a FAILURE marker
    after round 1 — the patched ``run_subagent`` (below) fails exactly ``1.1`` in
    round 1 and succeeds everything else.
    """

    def __init__(self) -> None:
        self._supervisor_calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        text = messages if isinstance(messages, str) else str(messages)
        if "You are the supervisor" in text:
            self._supervisor_calls += 1
            if self._supervisor_calls == 1:
                return AIMessage(
                    content=json.dumps(
                        {
                            "decision": "continue",
                            "subtasks": [
                                {"prompt": "investigate 0"},
                                {"prompt": "investigate 1"},
                            ],
                            "reason": "",
                        }
                    )
                )
            if self._supervisor_calls == 2:
                # Re-dispatch the failed round-1 subtask: redispatch=True + its id.
                # supervisor_node mints carry-forward only for a failed prior id, so
                # this keeps id "1.1" (NOT a fresh "2.0") in round 2.
                return AIMessage(
                    content=json.dumps(
                        {
                            "decision": "continue",
                            "subtasks": [
                                {"prompt": "retry 1", "subtask": "1.1", "redispatch": True}
                            ],
                            "reason": "retry the failure",
                        }
                    )
                )
            return AIMessage(
                content=json.dumps({"decision": "stop", "subtasks": [], "reason": "done"})
            )
        if "You are the writer" in text:
            return AIMessage(content="Final report citing [need a real id].")
        if "Write the research brief now" in text:
            return AIMessage(content="The research brief.")
        if "You are the scoping phase" in text:
            return AIMessage(content=json.dumps({"clear": True}))
        if "supported" in text.lower():
            return AIMessage(content=json.dumps({"supported": True}))
        return AIMessage(content=_FINDINGS)


@pytest.mark.asyncio
async def test_subagent_markers_carry_distinct_live_iteration_per_round():
    emit = _RecordingEmit()
    checkpointer = MemorySaver()
    model = _TwoRoundRedispatchModel()
    cfg = {
        "configurable": {
            "thread_id": "fanout-events-multiround-thread",
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
            # Injected ONCE as 0 and never advanced — the marker iteration must NOT
            # come from here (that was the defect). It must track live state.
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
        # Fail the round-1 subtask "1.1" so it can be re-dispatched in round 2;
        # everything else (including the round-2 retry of "1.1") succeeds.
        ns = kwargs.get("checkpoint_ns", "")
        if ns == "subagent:1.1" and "retry" not in str(prompt):
            return SubagentResult.failure("error", detail="boom")
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

    started = [d for t, d in emit.events if t == "subagent_started"]
    by_subtask = {}
    for d in started:
        by_subtask.setdefault(d["subtask"], []).append(d["iteration"])

    # Round 1 dispatched 1.0 and 1.1, both stamped with the live round 1.
    assert by_subtask["1.0"] == [1]
    # 1.1 dispatched in round 1 (iteration 1) AND re-dispatched in round 2: it keeps
    # its STABLE round-1 id "1.1" (no fresh "2.0") for cross-round linkage, but the
    # round-2 marker carries the CURRENT round 2 — NOT the original round, NOT 0.
    assert by_subtask["1.1"] == [1, 2]
    # No subtask id was minted under round 2 (the retry reused 1.1's id), so the
    # ONLY way round 2 shows up is via the iteration field — which it does.
    assert "2.0" not in by_subtask
    iterations_seen = {it for its in by_subtask.values() for it in its}
    assert iterations_seen == {1, 2}, iterations_seen

    # The supervisor_iteration markers (which always carried the correct round)
    # agree with the subagent markers now — the grouping mismatch is gone.
    sup_rounds = [
        d["iteration"]
        for t, d in emit.events
        if t == "supervisor_iteration" and d.get("decision") == "continue"
    ]
    assert sup_rounds == [1, 2]

    # The failed round-1 marker also carries the live round 1 (not 0).
    failed = [d for t, d in emit.events if t == "subagent_failed"]
    assert failed and all(d["subtask"] == "1.1" and d["iteration"] == 1 for d in failed)

    assert out.get("report")
