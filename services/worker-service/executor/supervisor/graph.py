"""Supervisor topology graph wiring — the Supervisor↔fan-out↔Supervisor sub-graph
(Task S6).

S6 supplies the **structural `Send` fan-out** and the **iteration conditional
edge**; it does NOT branch the runtime's top-level ``_build_graph`` nor compile
the production graph with ``durability="sync"`` — **that is S8's job**. S6
exposes :func:`add_supervisor_fanout`, which registers the Supervisor node, the
per-subtask fan-out node, the gather node, the fan-out map edge, and the
iteration conditional edge onto a caller-provided ``StateGraph`` so S8 can splice
it into the full ``Scope → Supervisor → … → Writer`` topology.

The wiring (this is the repo's FIRST structural ``Send``; verified against
``langgraph==1.0.5`` with a throwaway spike, 2026-06-06):

    supervisor ──(conditional `Send` map over `subtasks`)──► fanout (per subtask)
        ▲                                                        │
        │                                                        ▼
        └────────────── gather ◄──────────────────────────── (reducer merge)
                          │
                          └──(iteration conditional edge: continue → supervisor,
                                                           stop → writer/END)

Why a separate ``gather`` node sits between the fan-out and the iteration edge:
the iteration decision must be evaluated at the **fan-out super-step boundary**,
never mid-``Send``-branch (§A11-E2 — a mid-super-step exit would abandon live
sibling branches with partial ``pending_writes`` and re-bill completed siblings
on resume). LangGraph runs all ``Send`` branches of one super-step to completion
and merges their writes into ``subagent_results`` before the static
``fanout → gather`` edge fires, so ``gather`` (and the iteration edge after it)
runs strictly *after* the whole fan-out has checkpointed — exactly the boundary
the budget pause (S8) will also evaluate at. S6 does NOT implement the budget
loop; it only guarantees the loop structure checkpoints at that boundary.

Result gathering uses the ``subagent_results`` keyed reducer S5 declared and S6
completed (``state.py::_merge_subagent_results``): the fan-out node writes
``{subagent_results: {<subtask>: <result-dict>}}``; LangGraph merges every
branch's one-key delta into the channel keyed by ``subtask``, idempotently. A
re-dispatched subtask (carry-forward id, minted by ``supervisor_node``)
overwrites its own failure marker in place.

The per-subtask result-dict shape written to ``subagent_results``:
``{"ok": bool, "summary": str, "reason": str | None, "subtask": str}`` — a plain
JSON-serialisable projection of the helper's :class:`SubagentResult` (the
dataclass is not checkpoint-serialisable as-is, and a dict is what the Supervisor
prompt renderer + partial-failure split read).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import Send

from executor.subagents import SubagentResult, run_subagent
from executor.supervisor.nodes import (
    DECISION_CONTINUE,
    DECISION_STOP,
    parse_findings,
    supervisor_node,
)
from executor.supervisor.prompts import build_subagent_prompt
from executor.supervisor.state import SupervisorState

logger = logging.getLogger(__name__)


class SupervisorFanoutConfigError(RuntimeError):
    """Raised when the fan-out node is reached without its injected deps.

    The fan-out node runs ONE sub-agent via the shared ``run_subagent``, which
    needs a real ``model`` + ``checkpointer`` (the namespaced sub-checkpoint that
    gives per-inner-turn durability). S8 injects these via
    ``config["configurable"]["supervisor_fanout_deps"]``. If that injection is
    missing or incomplete, fail FAST with the missing key named — otherwise
    ``run_subagent(model=None, checkpointer=None)`` fails inside its blanket
    ``except`` → every branch returns an ``error`` marker → ``supervisor_node``
    raises ``SupervisorAllFailedError`` and dead-letters the task with a
    misleading "all sub-agents failed" reason that hides the real wiring bug.
    """

# Node names (the structural fan-out wiring; S8 references these when splicing).
SUPERVISOR_NODE_NAME = "supervisor"
FANOUT_NODE_NAME = "supervisor_fanout"
GATHER_NODE_NAME = "supervisor_gather"

# The Supervisor itself is depth 0; its first fan-out level is depth 1 (so the
# depth-2 cap enforced inside ``run_subagent`` is not ambiguously read as depth-3
# — §A11-E8 / §A4.1 S6 row).
SUPERVISOR_FANOUT_DEPTH = 1

# D1 (DEFERRED — wide-fan-out checkpoint payload cap/offload; design "Open
# decisions", plan §A8/D1). v1 mitigation is **log only**: when the serialized
# ``subagent_results`` payload exceeds this threshold, log a
# ``checkpoint.oversized`` warning so operators see the Postgres write/
# serialization pressure. The actual cap/offload is DEFERRED — do NOT truncate
# or offload state here. The threshold is sized below the worker's checkpoint
# write budget; tune with the S11 load-run max-bytes report.
OVERSIZED_PAYLOAD_THRESHOLD_BYTES = 1_000_000


def _result_to_dict(result: SubagentResult, subtask: str) -> dict:
    """Project a :class:`SubagentResult` into the checkpoint-serialisable dict
    the ``subagent_results`` channel stores (keyed by ``subtask``)."""
    return {
        "ok": result.ok,
        "summary": result.summary,
        "reason": result.reason,
        "subtask": subtask,
    }


def _maybe_log_oversized_payload(
    subagent_results: dict,
    *,
    threshold_bytes: int = OVERSIZED_PAYLOAD_THRESHOLD_BYTES,
) -> None:
    """D1 v1 mitigation — log (never cap) an oversized fan-out payload.

    A wide fan-out writes one large checkpoint per super-step holding all
    sub-agents' accumulated state. When the serialized ``subagent_results``
    exceeds ``threshold_bytes`` emit a ``checkpoint.oversized`` warning with the
    byte size. State is **not** truncated/offloaded — that cap is DEFERRED
    (§A8/D1, design "Open decisions")."""
    try:
        size = len(json.dumps(subagent_results, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return
    if size > threshold_bytes:
        logger.warning(
            "checkpoint.oversized subagent_results_bytes=%s threshold=%s "
            "subtask_count=%s (D1 deferred — logging only, state NOT truncated)",
            size,
            threshold_bytes,
            len(subagent_results),
        )


def _fanout_edge_factory(writer_node_name: str | None):
    """Build the conditional edge OUT of ``supervisor``.

    On ``continue`` it is the structural ``Send`` map: it reads the Supervisor's
    parsed ``subtasks`` and ``Send``s each (in parallel, in one super-step) to
    the per-subtask fan-out node — Pattern A (one ``thread_id`` / one task / one
    checkpoint stream). Each ``Send`` carries the subtask's stable ``subtask`` id
    + ``prompt`` (the fan-out node reads ``ceiling`` / ``depth`` / model /
    checkpointer from the injected config — they are graph-build constants, not
    per-branch payload).

    On ``stop`` (or an empty subtask list — a degenerate ``continue`` that
    decomposed to nothing) it routes straight to the Writer / ``END`` so the
    graph never strands on an empty ``Send`` list.
    """
    stop_target = writer_node_name or END

    def _fanout_edge(state: SupervisorState) -> Any:
        subtasks = state.get("subtasks") or []
        decision = state.get("supervisor_decision", DECISION_STOP)
        if decision != DECISION_CONTINUE or not subtasks:
            return stop_target
        return [
            Send(FANOUT_NODE_NAME, {"subtask": s["subtask"], "prompt": s["prompt"]})
            for s in subtasks
        ]

    return _fanout_edge


async def _fanout_node(state: dict, config: RunnableConfig) -> dict:
    """Per-subtask fan-out branch — runs ONE focused sub-agent via the SHARED
    helper (``run_subagent`` / ``build_subagent_node``) and writes its outcome
    into the ``subagent_results`` keyed reducer.

    ``state`` is the ``Send`` payload (``{subtask, prompt}``) — the sub-agent's
    entire input. The parent's ``messages`` are NOT passed in (context isolation
    holds at entry); the sub-agent's internal working turns live on the helper's
    separate channel — only this one keyed result crosses back.

    This REUSES S3's ``run_subagent`` exactly as S4's ReAct ``subagent_node``
    does — it does NOT fork a second subagent node. ``run_subagent`` returns a
    structured :class:`SubagentResult` (never raises a ceiling/timeout/depth/
    error out into the graph), so partial-failure handling reads markers from
    ``subagent_results`` in the next ``supervisor_node`` invocation.
    """
    configurable = (config or {}).get("configurable", {}) if config else {}
    subtask = state["subtask"]
    prompt = state.get("prompt", "") or ""

    # Fan-out deps are injected by S8 at graph-build (model / checkpointer /
    # ceiling / tools / emit). Fail FAST with the missing key named — a silent
    # ``run_subagent(model=None, ...)`` would surface as a phantom all-failed run
    # (see ``SupervisorFanoutConfigError``). Tests patch the module-level
    # ``run_subagent`` symbol (``executor.supervisor.graph.run_subagent``) — the
    # repo convention — so the call site below is identical in test and prod.
    deps = configurable.get("supervisor_fanout_deps") or {}
    for required in ("model", "checkpointer"):
        if deps.get(required) is None:
            raise SupervisorFanoutConfigError(
                f"supervisor fan-out deps not injected: {required} "
                f"(expected in config['configurable']['supervisor_fanout_deps'])"
            )

    sub_thread = f"{configurable.get('thread_id', '')}:subagent:{subtask}"

    # Wrap the Supervisor's focused sub-task instruction in S7's Subagent
    # findings template so the sub-agent emits structured
    # {claim, source_url, supporting_quote} findings (NOT freeform prose) — the
    # contract ``parse_findings`` + the citation binding consume.
    result = await run_subagent(
        build_subagent_prompt(prompt),
        deps.get("tools", []),
        ceiling=deps.get("ceiling"),
        depth=SUPERVISOR_FANOUT_DEPTH,
        model=deps["model"],
        checkpointer=deps["checkpointer"],
        thread_id=sub_thread,
        emit=deps.get("emit"),
    )

    # Parse the sub-agent's distilled summary into the parent ``findings`` channel
    # (S7). Only a successful sub-agent contributes findings; a failure marker
    # rides ``subagent_results`` and yields no findings (parse of a non-JSON /
    # empty summary returns []). ``findings`` is the append-only channel S5
    # declared — LangGraph concatenates every branch's list (§A0 inv. 4: the
    # append reducer never rewrites an existing entry; quotes are immutable).
    iteration = int(configurable.get("iteration", 0) or 0)
    findings: list[dict] = []
    if result.ok:
        findings = await parse_findings(
            result.summary,
            iteration=iteration,
            subtask=subtask,
            emit=deps.get("emit"),
        )

    logger.info(
        "supervisor.subagent_completed subtask=%s ok=%s reason=%s findings=%s",
        subtask,
        result.ok,
        result.reason,
        len(findings),
    )
    return {
        "subagent_results": {subtask: _result_to_dict(result, subtask)},
        "findings": findings,
    }


async def _gather_node(state: SupervisorState) -> dict:
    """Fan-out super-step boundary node.

    Runs strictly AFTER every ``Send`` branch of the round has completed and
    merged into ``subagent_results`` (LangGraph joins the parallel branches
    before the static ``fanout → gather`` edge fires). This is the boundary the
    iteration decision (and S8's budget pause) evaluate at — never mid-branch
    (§A11-E2). S6 writes nothing here except the D1 oversized-payload log; the
    iteration conditional edge after it reads the merged results.
    """
    _maybe_log_oversized_payload(state.get("subagent_results") or {})
    return {}


def _iteration_edge_factory(writer_node_name: str | None):
    """Build the iteration conditional edge OUT of ``gather``.

    Routes back to ``supervisor`` (continue) or to the Writer / ``END`` (stop).
    The routing key is the ``supervisor_decision`` the Supervisor node set on the
    PRECEDING super-step — so the loop re-enters ``supervisor`` only when the
    Supervisor chose ``continue``. (The Supervisor node itself owns the
    ``max_iterations`` cap and forces ``stop`` at it — see ``supervisor_node``.)
    """
    stop_target = writer_node_name or END

    def _iteration_edge(state: SupervisorState) -> str:
        decision = state.get("supervisor_decision", DECISION_STOP)
        if decision == DECISION_CONTINUE:
            return SUPERVISOR_NODE_NAME
        return stop_target

    return _iteration_edge


def add_supervisor_fanout(
    graph: StateGraph,
    *,
    writer_node_name: str | None = None,
) -> StateGraph:
    """Register the Supervisor↔fan-out↔Supervisor sub-wiring onto ``graph``.

    Adds the ``supervisor`` / ``supervisor_fanout`` / ``supervisor_gather`` nodes,
    the structural ``Send`` map edge (``supervisor`` → fan-out), the static
    ``fanout → gather`` join edge, and the iteration conditional edge
    (``gather`` → ``supervisor`` on continue, → ``writer_node_name``/``END`` on
    stop).

    The caller (S8) is responsible for:
      * adding the entry edge into ``SUPERVISOR_NODE_NAME`` (from ``scope`` /
        ``START``);
      * adding the Writer node (``writer_node_name``, S7) and its terminal edge;
      * compiling with ``durability="sync"`` and the shared checkpointer;
      * injecting ``supervisor_model`` / ``agent_config`` / ``supervisor_emit`` /
        ``supervisor_fanout_deps`` into ``config["configurable"]``.

    The ``writer_node_name`` seam: when ``None`` (S7's Writer not yet wired) the
    stop decision routes to ``END`` — a clean terminal seam S7/S8 replace with
    the real Writer target. S6 does NOT wire ``scope → supervisor`` or
    ``supervisor → writer`` end-to-end (S7/S8 own those).
    """
    graph.add_node(SUPERVISOR_NODE_NAME, supervisor_node)
    graph.add_node(FANOUT_NODE_NAME, _fanout_node)
    graph.add_node(GATHER_NODE_NAME, _gather_node)

    # The single stop target both conditional edges route to on ``stop``: the
    # Writer (S7) when wired, else ``END`` (the S7/S8 seam).
    stop_target = writer_node_name or END

    # Structural Send fan-out — the conditional edge returns a list of Sends on
    # ``continue`` and ``stop_target`` on ``stop`` (so an empty fan-out can't
    # strand the graph).
    graph.add_conditional_edges(
        SUPERVISOR_NODE_NAME,
        _fanout_edge_factory(writer_node_name),
        [FANOUT_NODE_NAME, stop_target],
    )
    # Static join: every fan-out branch funnels into the gather node, which runs
    # only after the whole super-step's branches merged (the boundary).
    graph.add_edge(FANOUT_NODE_NAME, GATHER_NODE_NAME)
    # Iteration conditional edge at the boundary (continue → supervisor, else
    # ``stop_target``).
    graph.add_conditional_edges(
        GATHER_NODE_NAME,
        _iteration_edge_factory(writer_node_name),
        [SUPERVISOR_NODE_NAME, stop_target],
    )
    return graph


__all__ = [
    "SUPERVISOR_NODE_NAME",
    "FANOUT_NODE_NAME",
    "GATHER_NODE_NAME",
    "SUPERVISOR_FANOUT_DEPTH",
    "OVERSIZED_PAYLOAD_THRESHOLD_BYTES",
    "SupervisorFanoutConfigError",
    "add_supervisor_fanout",
]
