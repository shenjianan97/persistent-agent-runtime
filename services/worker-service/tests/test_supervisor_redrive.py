"""S11 #5/#6 — resume-forward vs. operator-redrive over the COMPOSED Supervisor graph.

S6's ``test_supervisor_fanout.py::test_crash_resume_forward_restores_completed_siblings``
proves resume-forward at the *sub-wiring* level (a crashed branch re-runs; the
completed sibling is restored, not recomputed). S11 proves the LOAD-BEARING
*contrast* the design pins (design "Resume-forward vs. rollback — they are
different operations", §A11-E2) over the production ``build_supervisor_graph()``:

* **#5 crash resume-forward — REUSES.** A worker crash mid-fan-out resumes from
  the last super-step checkpoint: a completed sibling's ``run_subagent`` is NOT
  called again (its per-subtask run counter stays at 1); only the unfinished
  branch re-runs. ``subagent_results`` (the checkpointed keyed reducer) restores
  the completed entry.
* **#6 operator redrive — RECOMPUTES.** ``rollback_last_checkpoint`` (the design's
  redrive primitive) rolls the parent run back to a prior super-step and re-runs
  forward, so the WHOLE fan-out super-step re-executes — every sub-agent's
  ``run_subagent`` fires AGAIN (new tokens; the run counter increments past 1).
  Modelled here with LangGraph time-travel: re-invoking from a checkpoint config
  taken BEFORE the fan-out super-step forks a new run that recomputes that
  super-step (the same mechanism the worker's rollback path uses — re-run forward
  from a rolled-back checkpoint). Contrast with #5: resume-forward reuses,
  redrive recomputes.

All models are fakes; ``run_subagent`` is patched with a per-subtask call counter;
the graph runs on an in-process ``MemorySaver`` with ``durability="sync"``. No
Postgres, no TCP ports, no subprocess → worktree-concurrency-safe.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from executor.subagents import SubagentCeiling, SubagentResult
from executor.supervisor.graph import FANOUT_NODE_NAME, build_supervisor_graph


# Reuse the unified routing model from the partial-failure suite (one ``ainvoke``
# routed by prompt content — mirrors the live worker binding ONE model to all
# supervisor phases). Round 1 → continue with N subtasks; round 2 → stop.
from tests.test_supervisor_partial_failure import _RecordingEmit, _RoutingModel


_FINDINGS = json.dumps(
    {"findings": [{"claim": "c", "source_url": "https://ex.com", "supporting_quote": "q"}]}
)


def _config(*, n_subtasks: int, checkpointer, emit) -> dict:
    model = _RoutingModel(n_subtasks=n_subtasks)
    return {
        "configurable": {
            "thread_id": "redrive-thread",
            "scope_model": model,
            "supervisor_model": model,
            "writer_model": model,
            "verify_model": model,
            "agent_config": {
                "supervisor": {
                    "max_fanout_per_iteration": 5,
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


# --------------------------------------------------------------------------- #
# #5 — crash resume-forward over the composed graph REUSES completed siblings.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_crash_resume_forward_reuses_completed_siblings_composed():
    checkpointer = MemorySaver()
    cfg = _config(n_subtasks=2, checkpointer=checkpointer, emit=_RecordingEmit())
    run_counts: dict[str, int] = {}
    crash = {"on": True}

    async def fake_run(prompt, tools, **kwargs):
        which = "investigate 0" if "investigate 0" in prompt else "investigate 1"
        run_counts[which] = run_counts.get(which, 0) + 1
        if crash["on"] and which == "investigate 1":
            raise RuntimeError("SIMULATED worker crash mid-fan-out")
        return SubagentResult.success(_FINDINGS, usage={"input_tokens": 10, "output_tokens": 5})

    graph = build_supervisor_graph().compile(checkpointer=checkpointer)
    with patch(
        "executor.supervisor.graph.run_subagent",
        AsyncMock(side_effect=fake_run),
    ):
        with pytest.raises(RuntimeError, match="SIMULATED"):
            await graph.ainvoke(
                {"messages": [HumanMessage(content="research")]},
                config=cfg,
                durability="sync",
            )
        # Completed sibling persisted in the checkpoint reducer.
        snap = await graph.aget_state(cfg)
        assert "1.0" in snap.values["subagent_results"]

        # Resume forward — only the unfinished branch re-runs.
        crash["on"] = False
        out = await graph.ainvoke(None, config=cfg, durability="sync")

    # investigate 0 (completed) ran ONCE (restored, NOT recomputed); investigate 1
    # ran twice (crash + resume). Resume-forward REUSES.
    assert run_counts["investigate 0"] == 1
    assert run_counts["investigate 1"] == 2
    assert out.get("report")


# --------------------------------------------------------------------------- #
# #6 — operator redrive RECOMPUTES the rolled-back fan-out super-step.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_operator_redrive_recomputes_fanout_superstep():
    checkpointer = MemorySaver()
    cfg = _config(n_subtasks=2, checkpointer=checkpointer, emit=_RecordingEmit())
    run_counts: dict[str, int] = {}
    usage_deltas: list[dict] = []

    async def fake_run(prompt, tools, **kwargs):
        which = "investigate 0" if "investigate 0" in prompt else "investigate 1"
        run_counts[which] = run_counts.get(which, 0) + 1
        usage = {"input_tokens": 100, "output_tokens": 50}
        usage_deltas.append(usage)
        return SubagentResult.success(_FINDINGS, usage=usage)

    graph = build_supervisor_graph().compile(checkpointer=checkpointer)
    with patch(
        "executor.supervisor.graph.run_subagent",
        AsyncMock(side_effect=fake_run),
    ):
        # First clean run completes the whole graph.
        out1 = await graph.ainvoke(
            {"messages": [HumanMessage(content="research")]},
            config=cfg,
            durability="sync",
        )
        assert out1.get("report")
        # Each of the 2 subtasks ran exactly once on the first pass.
        assert run_counts == {"investigate 0": 1, "investigate 1": 1}
        first_pass_calls = len(usage_deltas)

        # Find a checkpoint taken strictly BEFORE the fan-out super-step (the
        # supervisor node's checkpoint — its next step is the Send fan-out). The
        # operator redrive rolls back to here and re-runs forward.
        pre_fanout_cfg = None
        async for snap in graph.aget_state_history(cfg):
            if FANOUT_NODE_NAME in (snap.next or ()):
                pre_fanout_cfg = snap.config
                break
        assert pre_fanout_cfg is not None, "no pre-fan-out checkpoint to roll back to"

        # Redrive: re-invoke from the rolled-back checkpoint → the fan-out
        # super-step re-runs forward (recompute = new tokens). The rolled-back
        # snapshot config carries the thread_id + checkpoint_id pointer; merge the
        # injected deps (models / fan-out deps) back in — the worker's rollback
        # path likewise re-runs ``execute_task`` (which re-injects the deps) on the
        # rolled-back thread.
        redrive_cfg = {
            "configurable": {
                **cfg["configurable"],
                **pre_fanout_cfg["configurable"],
            },
            "recursion_limit": 50,
        }
        out2 = await graph.ainvoke(None, config=redrive_cfg, durability="sync")

    # The whole fan-out super-step recomputed — both subtasks fired AGAIN.
    assert run_counts["investigate 0"] == 2, "redrive did not recompute investigate 0"
    assert run_counts["investigate 1"] == 2, "redrive did not recompute investigate 1"
    # New tokens were spent on the redrive (more run_subagent calls than the first
    # pass) — the design's "re-execution costs real new tokens" claim.
    assert len(usage_deltas) > first_pass_calls
    assert out2.get("report")
