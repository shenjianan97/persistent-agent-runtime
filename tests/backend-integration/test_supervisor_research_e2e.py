"""S11 REST E2E — research-preset create → submit → activity-tree projection.

Drives the FULL stack (live API + live worker + real Postgres) for a Supervisor
("Deep Research") run with a mocked LLM:

1. ``POST /v1/agents`` with the ``research`` preset → the server seeds
   ``topology=supervisor`` (S1/S2), so ``_build_graph`` compiles the Supervisor
   graph (S8) for this agent.
2. ``POST /v1/tasks`` → ONE task row; the worker fans out IN-PROCESS (Pattern A).
3. Poll ``GET /v1/tasks/{id}/activity`` → assert the tree-groupable markers
   (``marker.subagent.*`` / ``marker.supervisor.iteration``) project with
   ``iteration`` / ``subtask`` so the Console can group round → sub-agent.
4. Assert it is **ONE task row** — no sub-agent task rows in the task list
   (§A0.1 Pattern A: no parent_task_id tree, no per-sub-agent rows).

Behavioral assertions ONLY — NO Pattern-B assertions (no ``sub_agent_id`` /
``parent_task_id`` / ``waiting_for_subagent`` / per-sub-agent rows). All LLM calls
mocked at the worker boundary (the ``executor.providers.create_llm`` patch the
``e2e`` fixture installs); ``durability="sync"`` is the worker's compile default.

Run via the isolated harness: ``make e2e-test PYTEST_ARGS='-k supervisor_research_e2e'``.
Worktree-concurrency-safe: binds no ports, spawns no subprocess (the harness owns
the API/worker ports).
"""

from __future__ import annotations

import json

import pytest

from langchain_core.messages import AIMessage


TENANT_ID = "default"


class _ResearchRoutingModel:
    """One model object (the live worker binds ONE model to scope/supervisor/
    writer/verify + the fan-out sub-agents — see _inject_supervisor_configurable).
    ``ainvoke`` is routed by prompt content; every reply carries ``usage_metadata``
    so the run records realistic cost. ``bind_tools`` returns self so the sub-agent
    ReAct loop binds cleanly.

    Round 1 → continue with 3 subtasks; round 2 → stop → Writer. Each sub-agent
    emits one structured finding so parse_findings + the activity markers fire.
    """

    _USAGE = {"input_tokens": 40, "output_tokens": 20, "total_tokens": 60}

    def __init__(self):
        self._supervisor_calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        text = messages if isinstance(messages, str) else str(messages)
        if "You are the supervisor" in text:
            self._supervisor_calls += 1
            if self._supervisor_calls == 1:
                body = json.dumps(
                    {
                        "decision": "continue",
                        "subtasks": [{"prompt": f"investigate angle {i}"} for i in range(3)],
                        "reason": "gather evidence",
                    }
                )
            else:
                body = json.dumps({"decision": "stop", "subtasks": [], "reason": "sufficient"})
        elif "You are the writer" in text:
            body = "Final research report synthesising the findings [1.0-aaaaaaaa]."
        elif "Write the research brief now" in text:
            body = "Brief: investigate the topic thoroughly."
        elif "You are the scoping phase" in text:
            body = json.dumps({"clear": True})
        elif "supported" in text.lower():
            body = json.dumps({"supported": True})
        else:
            # Sub-agent: emit one structured finding (verbatim quote).
            body = json.dumps(
                {
                    "findings": [
                        {
                            "claim": "An evidenced claim.",
                            "source_url": "https://example.com/source",
                            "supporting_quote": "the exact supporting text",
                        }
                    ]
                }
            )
        return AIMessage(content=body, usage_metadata=dict(self._USAGE))


@pytest.mark.asyncio
async def test_research_preset_run_projects_tree_markers_one_task_row(e2e):
    e2e.use_llm(_ResearchRoutingModel())
    await e2e.start_worker("e2e-supervisor-research-worker")

    # 1. Create a research-preset agent — the server seeds topology=supervisor.
    agent = e2e.ensure_agent(
        agent_id="research-e2e-agent",
        display_name="Research E2E Agent",
        agent_config={
            "system_prompt": "Deep research.",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "temperature": 0.3,
            "preset": "research",
        },
        budget_max_per_task=1_000_000_000,
    )
    agent_id = agent["body"]["agent_id"]

    # The server derived the Supervisor topology from the preset (S1/S2).
    detail = e2e.api.get_agent(agent_id)["body"]
    assert detail["agent_config"].get("topology") == "supervisor", (
        f"research preset did not seed topology=supervisor: {detail['agent_config']}"
    )

    # 2. Submit a research task and let it complete.
    task_id = e2e.submit_task(agent_id=agent_id, input="Research the state of X.")
    await e2e.wait_for_status(task_id, "completed", timeout=90)

    # 3. Activity projection surfaces the tree-groupable markers.
    activity = e2e.api.get_activity(task_id, include_details=True)["body"]["events"]
    kinds = [ev["kind"] for ev in activity]

    subagent_started = [ev for ev in activity if ev["kind"] == "marker.subagent.started"]
    subagent_finding = [ev for ev in activity if ev["kind"] == "marker.subagent.finding"]
    supervisor_iter = [ev for ev in activity if ev["kind"] == "marker.supervisor.iteration"]

    assert subagent_started, f"no marker.subagent.started projected; kinds={kinds}"
    assert subagent_finding, f"no marker.subagent.finding projected; kinds={kinds}"
    assert supervisor_iter, f"no marker.supervisor.iteration projected; kinds={kinds}"

    # 3 sub-agents fanned out in round 1 with DISTINCT subtask ids.
    started_subtasks = {ev.get("subtask") for ev in subagent_started}
    assert len(started_subtasks) == 3, f"expected 3 distinct subtasks, got {started_subtasks}"
    # The subtask ids carry the round prefix (deterministic minting, §A11-E8).
    assert all(str(s).startswith("1.") for s in started_subtasks), started_subtasks

    # Each marker carries the iteration/subtask the Console groups round→sub-agent on.
    for ev in subagent_finding:
        assert ev.get("subtask") is not None
        assert ev.get("iteration") is not None
    for ev in supervisor_iter:
        assert ev.get("iteration") is not None

    # 4. ONE task row — no sub-agent task rows (Pattern A, §A0.1).
    tasks_body = e2e.api.list_tasks(agent_id=agent_id)["body"]
    all_tasks = tasks_body.get("items", tasks_body.get("tasks", []))
    task_ids = [t["task_id"] for t in all_tasks]
    assert task_ids == [task_id], (
        f"expected exactly one task row (the parent); got {task_ids} — "
        "a sub-agent task row would be a Pattern-B violation (§A0.1)"
    )

    # Defensive Pattern-A guards: no sub_agent_id / parent_task_id / waiting_for_subagent
    # anywhere in the projected activity or the task row.
    task_row = e2e.get_task(task_id)
    blob = json.dumps(activity) + json.dumps(task_row)
    for forbidden in ("sub_agent_id", "parent_task_id", "waiting_for_subagent"):
        assert forbidden not in blob, f"Pattern-B field {forbidden!r} leaked into the projection"
    assert task_row["status"] == "completed"
