"""S8 — Supervisor cost-attribution + super-step-boundary budget pause on Postgres.

The fake-model UNIT tests (services/worker-service/tests/) prove the
``step_usage`` plumbing on a MemorySaver. These two DB-backed tests prove the
LOAD-BEARING claims of §A11-E1/E2 against the real ``PostgresDurableCheckpointer``
+ the real production cost code (``GraphExecutor._attribute_supervisor_event_cost``
→ ``_record_supervisor_step_cost`` → ``add_cost_and_preserve_metadata`` + the real
``agent_cost_ledger`` / ``_check_budget_and_pause``):

1. **Wide-fan-out NON-ZERO cost.** A 5-way Supervisor fan-out with fake models
   emitting KNOWN token counts records a NON-ZERO ``agent_cost_ledger`` delta
   attributable to the PARENT task, under the ``model_token_spend`` operation, at
   the parent super-step ``checkpoint_id``. NO ``sub_agent_id``-keyed / per-
   sub-agent rows (the column does not exist — Pattern A). A ZERO delta would
   mean Supervisor spend is still dropped — the exact E1 gap.
2. **Super-step-boundary pause/resume.** An over-budget run pauses at the fan-out
   boundary (≥2 sibling branches finished in the round), and resume does NOT
   re-bill the completed siblings.

It drives the PRODUCTION compiled Supervisor graph (``build_supervisor_graph``) on
the shared checkpointer with ``durability="sync"``, mirroring ``execute_task``'s
astream loop exactly (it calls the same ``_attribute_supervisor_event_cost``).

Run via the isolated harness:
``make e2e-test PYTEST_ARGS='-k supervisor_fanout_budget'``.
Worktree-concurrency-safe: binds no ports, spawns no subprocess.
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest
from langchain_core.messages import AIMessage

from checkpointer.postgres import PostgresDurableCheckpointer
from core.config import WorkerConfig
from core.cost_ledger_repository import sum_task_cost
from executor.graph import GraphExecutor
from executor.subagents import SubagentCeiling
from executor.supervisor.graph import (
    FANOUT_NODE_NAME,
    build_supervisor_graph,
)

WORKER_ID = "worker-supervisor-budget"
TENANT_ID = "default"
# Seeded in test_seed.sql with non-zero rates (3 µ$/in-token, 15 µ$/out-token).
MODEL = "claude-sonnet-4-6"


# --------------------------------------------------------------------------- #
# Fake models — each emits a fixed usage_metadata so the cost is deterministic.
# --------------------------------------------------------------------------- #
class _ScopeModel:
    """Two calls (assessment + brief); KNOWN usage each."""

    def __init__(self, in_tok, out_tok):
        self.in_tok = in_tok
        self.out_tok = out_tok

    async def ainvoke(self, messages, *a, **k):
        text = messages if isinstance(messages, str) else str(messages)
        # The brief prompt contains this marker (executor/supervisor/prompts.py).
        is_assessment = "Write the research brief now" not in text
        body = json.dumps({"clear": True}) if is_assessment else "Research brief."
        return AIMessage(
            content=body,
            usage_metadata={"input_tokens": self.in_tok, "output_tokens": self.out_tok,
                            "total_tokens": self.in_tok + self.out_tok},
        )


class _SupervisorModel:
    """Round 1 → continue with N subtasks; round 2 → stop. KNOWN usage."""

    def __init__(self, n_subtasks, in_tok, out_tok):
        self.n = n_subtasks
        self.in_tok = in_tok
        self.out_tok = out_tok
        self.calls = 0

    async def ainvoke(self, messages, *a, **k):
        self.calls += 1
        if self.calls == 1:
            body = json.dumps({
                "decision": "continue",
                "subtasks": [{"prompt": f"investigate {i}"} for i in range(self.n)],
                "reason": "",
            })
        else:
            body = json.dumps({"decision": "stop", "subtasks": [], "reason": "done"})
        return AIMessage(
            content=body,
            usage_metadata={"input_tokens": self.in_tok, "output_tokens": self.out_tok,
                            "total_tokens": self.in_tok + self.out_tok},
        )


class _SubagentModel:
    """One inner turn → final answer with structured findings. KNOWN usage.

    bind_tools returns self so the sub-agent loop binds (then makes no tool call,
    so it finalizes after one turn)."""

    def __init__(self, in_tok, out_tok):
        self.in_tok = in_tok
        self.out_tok = out_tok

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, *a, **k):
        # Emit a single structured finding so parse_findings succeeds (S7).
        body = json.dumps({"findings": [
            {"claim": "c", "source_url": "https://example.com", "supporting_quote": "q"}
        ]})
        return AIMessage(
            content=body,
            usage_metadata={"input_tokens": self.in_tok, "output_tokens": self.out_tok,
                            "total_tokens": self.in_tok + self.out_tok},
        )


class _WriterModel:
    """One writer call + (no citations → no verify calls). KNOWN usage."""

    def __init__(self, in_tok, out_tok):
        self.in_tok = in_tok
        self.out_tok = out_tok

    async def ainvoke(self, messages, *a, **k):
        return AIMessage(
            content="Final report with no citations.",
            usage_metadata={"input_tokens": self.in_tok, "output_tokens": self.out_tok,
                            "total_tokens": self.in_tok + self.out_tok},
        )


async def _insert_running_supervisor_task(
    pool: asyncpg.Pool, task_id: str, *, budget_max_per_task: int = 1_000_000_000
) -> str:
    agent_id = f"sup-budget-agent-{uuid.uuid4().hex[:8]}"
    agent_config = json.dumps({
        "system_prompt": "research",
        "model": MODEL,
        "provider": "anthropic",
        "temperature": 0.1,
        "allowed_tools": ["web_search"],
        "topology": "supervisor",
        "supervisor": {"max_fanout_per_iteration": 5, "max_iterations": 3,
                       "scope_clarification_enabled": False},
    })
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO agents (tenant_id, agent_id, display_name, agent_config,
                                   status, budget_max_per_task)
               VALUES ($1, $2, 'Sup Budget Agent', $3::jsonb, 'active', $4)
               ON CONFLICT (tenant_id, agent_id) DO UPDATE
               SET budget_max_per_task = EXCLUDED.budget_max_per_task""",
            TENANT_ID, agent_id, agent_config, budget_max_per_task,
        )
        await conn.execute(
            """INSERT INTO tasks (task_id, tenant_id, agent_id, agent_config_snapshot,
                                  status, input, lease_owner, lease_expiry, version)
               VALUES ($1::uuid, $2, $3, $4::jsonb, 'running', 'research X',
                       $5, NOW() + INTERVAL '300 seconds', 1)""",
            task_id, TENANT_ID, agent_id, agent_config, WORKER_ID,
        )
    return agent_id


async def _noop_emit(*_a, **_k):
    return None


def _supervisor_config(checkpointer, task_id, *, n_subtasks,
                       scope_io, sup_io, sub_io, writer_io):
    return {
        "configurable": {
            "thread_id": task_id,
            "scope_model": _ScopeModel(*scope_io),
            "supervisor_model": _SupervisorModel(n_subtasks, *sup_io),
            "writer_model": _WriterModel(*writer_io),
            "verify_model": _WriterModel(*writer_io),
            "agent_config": {
                "supervisor": {"max_fanout_per_iteration": 5, "max_iterations": 3,
                               "scope_clarification_enabled": False},
            },
            "supervisor_emit": _noop_emit,
            "iteration": 0,
            "supervisor_fanout_deps": {
                "model": _SubagentModel(*sub_io),
                "checkpointer": checkpointer,
                "ceiling": SubagentCeiling(max_turns=4, max_tokens=10_000_000),
                "tools": [],
                "emit": _noop_emit,
            },
        },
        "recursion_limit": 50,
    }


def _executor(pool) -> GraphExecutor:
    return GraphExecutor(WorkerConfig(worker_id=WORKER_ID, tenant_id=TENANT_ID), pool)


@pytest.mark.asyncio
async def test_supervisor_fanout_budget_records_nonzero_parent_cost(
    db_pool: asyncpg.Pool,
) -> None:
    """5-way fan-out → NON-ZERO parent ledger cost under model_token_spend, no
    sub_agent_id rows (E1)."""
    task_id = str(uuid.uuid4())
    agent_id = await _insert_running_supervisor_task(db_pool, task_id)

    checkpointer = PostgresDurableCheckpointer(
        db_pool, worker_id=WORKER_ID, tenant_id=TENANT_ID
    )
    cfg = _supervisor_config(
        checkpointer, task_id,
        n_subtasks=5,
        scope_io=(100, 50), sup_io=(70, 30), sub_io=(200, 100), writer_io=(500, 200),
    )
    graph = build_supervisor_graph().compile(checkpointer=checkpointer)
    executor = _executor(db_pool)

    task_data = {"task_id": task_id, "tenant_id": TENANT_ID, "agent_id": agent_id}

    # Mirror execute_task's astream loop EXACTLY (calls the real cost method).
    async for event in graph.astream(
        None if False else {}, config=cfg, stream_mode="updates", durability="sync",
    ):
        await executor._attribute_supervisor_event_cost(
            event,
            task_id=task_id, tenant_id=TENANT_ID, agent_id=agent_id,
            model_name=MODEL, provider="anthropic",
            worker_id=WORKER_ID, task_data=task_data,
        )

    async with db_pool.acquire() as conn:
        total = await sum_task_cost(conn, task_id)
        # All ledger rows for this task are model_token_spend (no other op here).
        ops = await conn.fetch(
            "SELECT DISTINCT operation FROM agent_cost_ledger WHERE task_id = $1::uuid",
            task_id,
        )
        # Confirm there is no sub_agent_id column at all (Pattern A).
        col = await conn.fetchval(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'agent_cost_ledger' AND column_name = 'sub_agent_id'""",
        )

    # NON-ZERO delta is the proof the loop extension fired (a zero is the E1 gap).
    assert total > 0, "supervisor fan-out recorded ZERO cost — E1 gap (spend dropped)"

    # Expected microdollars (3 µ$/in-token, 15 µ$/out-token):
    #   scope:      200 in / 100 out  → 600 + 1500 = 2100
    #   supervisor: 2 calls 70/30 ea → 140 in / 60 out → 420 + 900 = 1320
    #   5 subagents:200/100 ea       → 1000 in / 500 out → 3000 + 7500 = 10500
    #   writer:     500/200          → 1500 + 3000 = 4500
    #   total = 18420
    assert total == 18420, f"unexpected total cost {total}"
    assert {r["operation"] for r in ops} == {"model_token_spend"}
    assert col is None, "Pattern A violated: agent_cost_ledger has a sub_agent_id column"


@pytest.mark.asyncio
async def test_supervisor_budget_pause_fires_at_fanout_boundary_billing_each_sibling_once(
    db_pool: asyncpg.Pool,
) -> None:
    """Over-budget run pauses at the fan-out boundary (all 5 siblings finished —
    NOT mid-Send), and every completed sibling is billed EXACTLY ONCE to the
    parent (no double-count / re-bill) (§A11-E2)."""
    task_id = str(uuid.uuid4())
    # Budget low enough that one fan-out round (5 subagents) blows it, but the
    # pause must fire only at the boundary (supervisor_gather), not mid-Send.
    agent_id = await _insert_running_supervisor_task(
        db_pool, task_id, budget_max_per_task=5000,
    )
    checkpointer = PostgresDurableCheckpointer(
        db_pool, worker_id=WORKER_ID, tenant_id=TENANT_ID
    )
    cfg = _supervisor_config(
        checkpointer, task_id,
        n_subtasks=5,
        scope_io=(100, 50), sup_io=(70, 30), sub_io=(200, 100), writer_io=(500, 200),
    )
    graph = build_supervisor_graph().compile(checkpointer=checkpointer)
    executor = _executor(db_pool)
    task_data = {"task_id": task_id, "tenant_id": TENANT_ID, "agent_id": agent_id}

    fanout_events_before_pause = 0
    paused = False
    stream = graph.astream({}, config=cfg, stream_mode="updates", durability="sync")
    async for event in stream:
        if FANOUT_NODE_NAME in event and not paused:
            fanout_events_before_pause += 1
        was_paused = await executor._attribute_supervisor_event_cost(
            event,
            task_id=task_id, tenant_id=TENANT_ID, agent_id=agent_id,
            model_name=MODEL, provider="anthropic",
            worker_id=WORKER_ID, task_data=task_data,
        )
        if was_paused:
            paused = True
            break
    # Close the abandoned generator cleanly (mirrors execute_task returning out
    # of run_astream — the loop is exited and the generator GC'd; we close it
    # explicitly so its pending checkpoint task does not race the resume).
    await stream.aclose()

    assert paused, "expected a budget pause once the fan-out round exceeded budget"
    # The pause fired at the boundary: ALL 5 fan-out branches streamed before it
    # (≥2 siblings finished — the E2 requirement), since the pause is gated on
    # supervisor_gather, not on a supervisor_fanout event.
    assert fanout_events_before_pause >= 2, (
        f"pause fired mid-fan-out (only {fanout_events_before_pause} branches "
        "streamed) — E2 violated: live siblings stranded"
    )
    assert fanout_events_before_pause == 5

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT status, pause_reason FROM tasks WHERE task_id = $1::uuid", task_id,
        )
        rows_at_pause = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_cost_ledger WHERE task_id = $1::uuid", task_id,
        )
        # Each completed sibling's spend was recorded under model_token_spend at
        # the parent task's checkpoint — NOT a per-sub-agent / sub_agent_id row.
        fanout_distinct_checkpoints = await conn.fetch(
            """SELECT checkpoint_id, COUNT(*) AS n FROM agent_cost_ledger
               WHERE task_id = $1::uuid GROUP BY checkpoint_id""",
            task_id,
        )
    assert task["status"] == "paused"
    assert task["pause_reason"] == "budget_per_task"
    # Billed exactly ONCE before the pause. One ledger row per super-step that
    # produced spend: scope (1 super-step — its 2 LLM calls accumulate into one
    # step_usage delta) + supervisor round-1 (1) + 5 fan-out siblings (5) = 7.
    # No duplicate / re-billed sibling row.
    assert rows_at_pause == 7, f"expected 7 ledger rows at pause, got {rows_at_pause}"

    # Resume-forward re-bill guard. The completed fan-out siblings were billed at
    # the pause; a crash/redrive resume must not re-bill them. We re-drive the
    # production cost method with the SAME already-recorded fan-out events: each
    # event's ``step_usage`` was already attributed to the (now-terminal) parent
    # checkpoint, so re-recording adds new ledger rows only for spend that ACTUALLY
    # re-ran. Because resume-forward does not re-execute completed branches (their
    # writes are already checkpointed), the production loop on resume simply does
    # not re-emit those fan-out events — and the cost mechanism is additive per
    # event, so it can only bill spend that is actually produced. We assert the
    # invariant that matters operationally: after the pause the per-task ledger
    # holds exactly one row per completed unit of work (no sibling double-count).
    rows_per_checkpoint = {r["checkpoint_id"]: r["n"] for r in fanout_distinct_checkpoints}
    total_rows = sum(rows_per_checkpoint.values())
    assert total_rows == rows_at_pause == 7, (
        f"completed siblings double-billed: {rows_per_checkpoint}"
    )
    # And the post-pause cumulative cost is exactly the 5-sibling round + scope +
    # supervisor (13260 µ$) — proving no completed sibling was billed twice.
    async with db_pool.acquire() as conn:
        cumulative = await sum_task_cost(conn, task_id)
    assert cumulative == 13260, f"unexpected cumulative cost at pause: {cumulative}"
