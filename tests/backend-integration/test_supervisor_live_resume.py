"""S11 (A) — LIVE-worker resume of a PAUSED Supervisor task through execute_task.

This is the highest-risk S11 item (S8's PREREQUISITE note). S8 proved the
boundary-pause + bill-once contract at the GRAPH layer on real Postgres
(``test_supervisor_fanout_budget.py``), but did NOT run a real ``execute_task``-level
resume of a *paused* Supervisor task through the LIVE worker. The unverified link:
resume rebuilds the graph via ``_build_graph`` (re-registering the supervisor
nodes) and MUST re-run ``_inject_supervisor_configurable`` so
``supervisor_fanout_deps`` / the four models are present on the resumed run — else
the resumed fan-out fails fast (``SupervisorFanoutConfigError``) or LangGraph logs
``Ignoring unknown node name supervisor_fanout`` and the lease is revoked.

This test exercises that path end-to-end:

1. Create a ``research``-preset agent (server seeds ``topology=supervisor``) with a
   per-task budget low enough that one fan-out round trips it.
2. Submit a research task; the LIVE worker runs Scope → Supervisor → fan-out and
   the real cost mechanism PAUSES it at the fan-out super-step boundary
   (``pause_reason=budget_per_task``).
3. Raise the budget; ``POST /resume`` → the worker re-claims, ``execute_task``
   rebuilds + re-injects the supervisor config, and the run RESUMES FORWARD to a
   terminal ``completed`` (Scope → Writer).
4. Assert it completed, the report exists, and the completed siblings were NOT
   re-billed on resume (cumulative cost grows only by the post-pause work, the
   fan-out siblings billed exactly once).

If the config-reinjection gap is real, this test FAILS at step 3 (the task does
not reach ``completed`` — it re-pauses, dead-letters, or the worker logs the
unknown-node warning). That is a REAL S8 defect — reported, not papered over.

All LLM mocked at the worker boundary; ``durability="sync"``; the harness owns the
API/worker ports. Run via: ``make e2e-test PYTEST_ARGS='-k supervisor_live_resume'``.
"""

from __future__ import annotations

import json

import pytest

from langchain_core.messages import AIMessage


TENANT_ID = "default"


class _ResumeRoutingModel:
    """One model bound to all supervisor phases (mirrors the live worker). Every
    reply carries ``usage_metadata`` so the real cost mechanism accrues spend and
    trips the budget pause.

    The Supervisor decision is **state-routed, NOT call-count-routed**: it emits
    ``continue`` (fan out 3 subtasks) only while ``subagent_results`` is empty, and
    ``stop`` once any round-1 result is present. This is load-bearing for the
    resume test: ``execute_task`` rebuilds a FRESH model instance on resume, so a
    call-counter would reset and spuriously re-fan-out a NEW round (billing new,
    legitimate tokens) — masking whether resume-forward preserved the completed
    siblings. Routing on the rendered ``subagent_results`` block (which the
    checkpointed reducer restores on resume — "<subtask>: ok — …") mirrors how a
    real LLM decides and makes the resume STOP at the Writer, exercising the pure
    resume-forward path."""

    _USAGE = {"input_tokens": 200, "output_tokens": 100, "total_tokens": 300}

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        text = messages if isinstance(messages, str) else str(messages)
        if "You are the supervisor" in text:
            # Round-1 results already present (restored from the checkpointed
            # subagent_results reducer) → STOP. Empty → fan out round 1.
            if "1.0: ok" in text:
                body = json.dumps({"decision": "stop", "subtasks": [], "reason": "sufficient"})
            else:
                body = json.dumps(
                    {
                        "decision": "continue",
                        "subtasks": [{"prompt": f"angle {i}"} for i in range(3)],
                        "reason": "gather",
                    }
                )
        elif "You are the writer" in text:
            body = "Final report [1.0-aaaaaaaa]."
        elif "Write the research brief now" in text:
            body = "Brief."
        elif "You are the scoping phase" in text:
            body = json.dumps({"clear": True})
        elif "supported" in text.lower():
            body = json.dumps({"supported": True})
        else:
            body = json.dumps(
                {
                    "findings": [
                        {
                            "claim": "c",
                            "source_url": "https://example.com",
                            "supporting_quote": "the exact text",
                        }
                    ]
                }
            )
        return AIMessage(content=body, usage_metadata=dict(self._USAGE))


async def _sum_task_cost(e2e, task_id: str) -> int:
    return (
        await e2e.db.fetchval(
            "SELECT COALESCE(SUM(cost_microdollars), 0) FROM agent_cost_ledger "
            "WHERE task_id = $1::uuid",
            task_id,
        )
        or 0
    )


async def _ledger_row_count(e2e, task_id: str) -> int:
    return (
        await e2e.db.fetchval(
            "SELECT COUNT(*) FROM agent_cost_ledger WHERE task_id = $1::uuid", task_id
        )
        or 0
    )


@pytest.mark.asyncio
async def test_paused_supervisor_task_resumes_through_execute_task(e2e):
    e2e.use_llm(_ResumeRoutingModel())
    await e2e.start_worker("e2e-supervisor-live-resume-worker")

    # Budget tuned so the pause fires at the FAN-OUT super-step boundary (the
    # supervisor_gather node, after all 3 siblings finished + merged into
    # subagent_results) — NOT at the earlier supervisor-node boundary (which would
    # pause before any sibling ran, making "resume re-bill" untestable). Each LLM
    # call is 200in/100out → 600 + 1500 = 2100 µ$ (seeded rates 3 µ$/in, 15 µ$/out):
    #   scope (2 calls, 1 super-step) 4200 + supervisor round-1 (1) 2100 → 6300
    #   cumulative after the SUPERVISOR boundary (must be UNDER budget so it does
    #   not pause here); + 3 fan-out siblings (3 × 2100 = 6300) → 12600 cumulative
    #   at the GATHER boundary (must be OVER budget → pause here). Budget 9000 sits
    #   between 6300 and 12600, so the pause fires only after the siblings complete.
    agent = e2e.ensure_agent(
        agent_id="research-resume-agent",
        display_name="Research Resume Agent",
        agent_config={
            "system_prompt": "Deep research.",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "temperature": 0.3,
            "preset": "research",
        },
        budget_max_per_task=9000,
    )
    agent_id = agent["body"]["agent_id"]
    detail = e2e.api.get_agent(agent_id)["body"]
    assert detail["agent_config"].get("topology") == "supervisor"

    task_id = e2e.submit_task(agent_id=agent_id, input="Research the state of X.")

    # The LIVE worker runs Scope → Supervisor → fan-out and the real cost mechanism
    # pauses at the fan-out boundary.
    await e2e.wait_for_status(task_id, "paused", timeout=90)
    paused = e2e.get_task(task_id)
    assert paused["pause_reason"] == "budget_per_task", (
        f"expected a per-task budget pause, got {paused.get('pause_reason')}"
    )

    cost_at_pause = await _sum_task_cost(e2e, task_id)
    rows_at_pause = await _ledger_row_count(e2e, task_id)
    assert cost_at_pause > 0, "no cost recorded before the pause — E1 cost gap"

    # The pause fired at the FAN-OUT boundary: all 3 round-1 siblings completed,
    # emitted their findings, and were billed once (cost_at_pause includes a full
    # fan-out round). Assert the round-1 sub-agent markers are present (the
    # completed work the resume must restore, not recompute).
    pause_activity = e2e.api.get_activity(task_id, include_details=True)["body"]["events"]
    pause_subtasks = {
        ev.get("subtask")
        for ev in pause_activity
        if ev["kind"] == "marker.subagent.finding"
    }
    assert pause_subtasks == {"1.0", "1.1", "1.2"}, (
        f"expected the fan-out boundary pause to have completed round-1 siblings; "
        f"got finding subtasks {pause_subtasks} — budget tripped at the wrong boundary"
    )

    # Resume while still over budget → 409 (sanity: the pause is real).
    from helpers.api_client import ApiError

    with pytest.raises(ApiError) as exc:
        e2e.resume_task(task_id)
    assert exc.value.status_code == 409

    # Raise the budget and resume through the LIVE worker. THIS is the unverified
    # link: execute_task must rebuild the supervisor graph AND re-inject the
    # supervisor config (models + supervisor_fanout_deps) on the resumed run.
    e2e.api.update_agent(agent_id, budget_max_per_task=1_000_000_000)
    result = e2e.resume_task(task_id)
    assert result["status"] == "queued"

    # If config-reinjection is missing, the resumed fan-out fails fast
    # (SupervisorFanoutConfigError → the task dead-letters / re-pauses, never
    # reaching completed). A clean completion proves the reinjection holds.
    await e2e.wait_for_status(task_id, "completed", timeout=120)

    final = e2e.get_task(task_id)
    assert final["status"] == "completed"

    # The completed run produced a report (Scope → ... → Writer ran end-to-end).
    activity = e2e.api.get_activity(task_id, include_details=True)["body"]["events"]
    assert any(ev["kind"] == "marker.supervisor.iteration" for ev in activity)

    # Resume-forward did NOT re-bill the completed fan-out siblings: the cost grew
    # only by the post-pause work (round-2 supervisor decision + Writer), and the
    # pre-pause sibling ledger rows were not duplicated.
    cost_after = await _sum_task_cost(e2e, task_id)
    rows_after = await _ledger_row_count(e2e, task_id)
    assert cost_after >= cost_at_pause, "cumulative cost should be monotonic"
    # The pre-pause rows are unchanged (additive, no overwrite/duplicate); resume
    # adds rows only for the genuinely new post-pause super-steps.
    assert rows_after >= rows_at_pause
    # The 3 fan-out siblings completed + merged + were BILLED before the pause
    # (cost_at_pause includes one full fan-out round = 3 × 2100 = 6300 µ$). On
    # resume, the state-routed Supervisor sees the restored round-1 results and
    # STOPS → only the round-2 supervisor decision + the Writer run (and an unbilled
    # verify of an unresolvable citation). The completed siblings are NOT
    # recomputed/re-billed: the post-resume increment is at most a couple of
    # single-node super-steps (~2 × 2100), well under one full fan-out round.
    increment = cost_after - cost_at_pause
    assert increment < 6300, (
        f"resume re-billed the completed fan-out round (increment {increment} µ$ ≥ "
        "one full fan-out round = 6300 µ$) — resume-forward did NOT restore the "
        "completed siblings (config-reinjection / pending-Send resume defect)"
    )

    # Pattern A: still exactly one task row, no sub-agent rows.
    tasks_body = e2e.api.list_tasks(agent_id=agent_id)["body"]
    items = tasks_body.get("items", tasks_body.get("tasks", []))
    assert [t["task_id"] for t in items] == [task_id]
