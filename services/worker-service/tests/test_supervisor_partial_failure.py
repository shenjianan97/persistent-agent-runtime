"""S11 #2/#3 — partial-subagent-failure behavior over the COMPOSED Supervisor graph.

S6's ``test_supervisor_fanout.py`` proves partial-failure *at the
``supervisor_node`` level* (the node raises ``SupervisorAllFailedError`` on a
zero-progress round; ≥1 success proceeds). S11 proves the same two behaviors
**end-to-end through the production ``build_supervisor_graph()``** (Scope →
Supervisor → fan-out → gather → Writer), which is the composed surface S6's
isolated-node tests cannot cover:

* **#2 partial failure → proceed.** One sub-agent of N fails (its
  ``run_subagent`` returns a failure marker); the graph PROCEEDS — a
  ``subagent_failed{reason}`` event is emitted for the failed subtask, findings
  from the surviving sub-agents reach the Writer, the Writer runs, and the run
  reaches a terminal ``report`` (does NOT error).
* **#3 zero return → fail.** ALL sub-agents in a round fail → the graph reaches a
  terminal FAILURE (``SupervisorAllFailedError`` raised out of the graph), NOT a
  silent empty report. The all-failed case is in-graph terminal failure, never a
  dead-lettered sub-task (Pattern A — §A0.1).

All models are fakes; the sub-agent run is a patched ``run_subagent``; the graph
runs on an in-process ``MemorySaver`` with ``durability="sync"`` (the worker
convention). No Postgres, no TCP ports, no subprocess → worktree-concurrency-safe.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from executor.subagents import SubagentCeiling, SubagentResult
from executor.supervisor.graph import build_supervisor_graph
from executor.supervisor.nodes import SupervisorAllFailedError


# --------------------------------------------------------------------------- #
# A single unified fake model — one ``ainvoke`` routed by prompt content, mirroring
# how the live worker binds ONE model object to scope/supervisor/writer/verify
# (executor/graph.py::_inject_supervisor_configurable). bind_tools returns self so
# the sub-agent ReAct loop binds cleanly.
# --------------------------------------------------------------------------- #
class _RoutingModel:
    def __init__(self, *, n_subtasks: int):
        self.n = n_subtasks
        self._supervisor_calls = 0

    def bind_tools(self, tools, **kwargs):  # sub-agent loop binds tools
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        text = messages if isinstance(messages, str) else str(messages)
        # Supervisor: round 1 → continue with N subtasks; round 2 → stop. Checked
        # FIRST (the supervisor prompt also mentions "research brief").
        if "You are the supervisor" in text:
            self._supervisor_calls += 1
            if self._supervisor_calls == 1:
                return AIMessage(
                    content=json.dumps(
                        {
                            "decision": "continue",
                            "subtasks": [
                                {"prompt": f"investigate {i}"} for i in range(self.n)
                            ],
                            "reason": "",
                        }
                    )
                )
            return AIMessage(
                content=json.dumps({"decision": "stop", "subtasks": [], "reason": "done"})
            )
        # Writer.
        if "You are the writer" in text:
            return AIMessage(content="Final report citing [need a real id].")
        # Scope: brief generation.
        if "Write the research brief now" in text:
            return AIMessage(content="The research brief.")
        # Scope: clarity assessment (return clear → no interrupt).
        if "You are the scoping phase" in text:
            return AIMessage(content=json.dumps({"clear": True}))
        # Verify (judges quote-supports-sentence).
        if "supported" in text.lower():
            return AIMessage(content=json.dumps({"supported": True}))
        # Sub-agent (Sub-task: …) — but the failing/succeeding behaviour is handled
        # by the patched run_subagent, so this is only reached if a real sub-agent
        # loop runs. Return structured findings.
        return AIMessage(
            content=json.dumps(
                {
                    "findings": [
                        {
                            "claim": "c",
                            "source_url": "https://example.com",
                            "supporting_quote": "q",
                        }
                    ]
                }
            )
        )


def _config(*, n_subtasks: int, emit) -> dict:
    model = _RoutingModel(n_subtasks=n_subtasks)
    return {
        "configurable": {
            "thread_id": "partial-failure-thread",
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
                "checkpointer": MemorySaver(),
                "ceiling": SubagentCeiling(max_turns=4, max_tokens=10_000),
                "tools": [],
                "emit": emit,
            },
        },
        "recursion_limit": 50,
    }


class _RecordingEmit:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, details: dict):
        self.events.append((event_type, details))


# --------------------------------------------------------------------------- #
# #2 — one of three sub-agents fails → the run proceeds to a Writer report.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_partial_failure_one_subagent_fails_writer_still_runs():
    emit = _RecordingEmit()
    cfg = _config(n_subtasks=3, emit=emit)

    findings_summary = json.dumps(
        {
            "findings": [
                {"claim": "c", "source_url": "https://ex.com", "supporting_quote": "q"}
            ]
        }
    )

    async def fake_run(prompt, tools, **kwargs):
        # The middle subtask fails; the others return structured findings.
        if "investigate 1" in prompt:
            return SubagentResult.failure("timeout")
        return SubagentResult.success(findings_summary)

    graph = build_supervisor_graph().compile(checkpointer=MemorySaver())
    with patch(
        "executor.supervisor.graph.run_subagent",
        AsyncMock(side_effect=fake_run),
    ):
        out = await graph.ainvoke(
            {"messages": [HumanMessage(content="research the thing")]},
            config=cfg,
            durability="sync",
        )

    # The run PROCEEDED — a terminal report exists (NOT an errored run).
    assert out.get("report"), "Writer did not produce a report despite ≥1 success"
    # A subagent_failed{reason} event was emitted for the failed subtask.
    failed = [e for e in emit.events if e[0] == "subagent_failed"]
    assert failed, "expected a subagent_failed event for the failed sub-agent"
    assert any(d.get("reason") == "timeout" for _, d in failed)
    # Findings from the surviving sub-agents reached the Writer (2 of 3 succeeded).
    results = out["subagent_results"]
    ok = [k for k, v in results.items() if v.get("ok")]
    failed_results = [k for k, v in results.items() if not v.get("ok")]
    assert len(ok) == 2
    assert len(failed_results) == 1


# --------------------------------------------------------------------------- #
# #3 — ALL sub-agents fail → terminal failure (not a silent empty report).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_zero_return_all_fail_reaches_terminal_failure():
    emit = _RecordingEmit()
    cfg = _config(n_subtasks=3, emit=emit)

    async def fake_run(prompt, tools, **kwargs):
        return SubagentResult.failure("error")

    graph = build_supervisor_graph().compile(checkpointer=MemorySaver())
    with patch(
        "executor.supervisor.graph.run_subagent",
        AsyncMock(side_effect=fake_run),
    ):
        with pytest.raises(SupervisorAllFailedError):
            await graph.ainvoke(
                {"messages": [HumanMessage(content="research the thing")]},
                config=cfg,
                durability="sync",
            )

    # The all-failed case surfaced as in-graph terminal failure — every sub-agent
    # has a failure marker, and there is NO silent empty report path.
    failed = [e for e in emit.events if e[0] == "subagent_failed"]
    assert len(failed) == 3
