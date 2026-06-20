"""Shared in-process fan-out helper — ``run_subagent`` (Agent Modes, Pattern A).

This is the **single** primitive both delegation drivers route through (the
``dispatch_subagent`` tool — S4 — and the Supervisor graph — S6). Built once,
used by both; neither driver forks a second copy.

Load-bearing wiring (verified against ``langgraph==1.0.5``; spikes #2–#5,
2026-06-05): a sub-agent is a **compiled ReAct subgraph declared as a node**,
reached from the parent graph **via ``Send``**, **sharing the parent
checkpointer** (a namespaced sub-checkpoint per branch). That — *not* an
imperatively ``ainvoke``d subgraph — is what gives:

* **per-inner-turn crash resume** — a worker crash mid-sub-agent resumes the
  parent's run at the inner turn it died on, restoring earlier turns rather
  than re-spending their tokens; and
* a **persisted sub-agent transcript** — the sub-agent's working messages are
  checkpointed under its ``subagent:<id>`` namespace (the E5 / Console drill-in
  read path), reachable from the shared checkpointer via ``alist`` filtered by
  ``checkpoint_ns``.

The helper itself owns the **per-sub-agent guardrails** (in graph state, never
the task/lease layer):

* a **token + turn ceiling** — on exhaustion the call STOPS and returns a
  ``ceiling`` failure marker (never raises);
* a **wall-clock timeout** — on expiry returns a ``timeout`` failure marker;
* a **depth counter** capped at :data:`MAX_SUBAGENT_DEPTH` — ``depth`` over the
  cap returns a ``depth`` failure marker with no spawn;
* a **headless filter** — any ``interrupt()``-bearing tool
  (``request_human_input`` and any future pause tool) is dropped from the
  allowlist before binding, because ≥2 sub-agents calling ``interrupt()`` in
  one ``Send`` super-step makes the worker's scalar ``Command(resume=...)``
  path raise ``RuntimeError`` (verified, LangGraph 1.0.5); and
* a **``subagent.heartbeat`` span event** emitted via the injected ``emit``
  callable — the design's *only* wedged-branch detector (the parent lease stays
  healthy while it ``await``s the fan-out, so silence is the only signal). It
  is a Langfuse span event ONLY: never an append-only task-event row, never a
  lease-expiry touch, never a call into the worker's lease-heartbeat manager.

Failure is **always a returned value** (:class:`SubagentResult` with a
``reason``), never an exception that aborts the parent graph — the driver (S4
threads a ``ToolMessage``; S6 merges into its results reducer) decides what to
do with it.

Cost rolls into the parent (Pattern A): no per-sub-agent cost-ledger row, and
none of Pattern B's cross-task spawn-and-await identifiers exist here (this
module is verified free of them by ``test_no_pattern_b_symbols_in_module``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, Awaitable, Callable, Iterable, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import RetryPolicy, Send

from executor.retry_classification import is_retryable_error
from executor.text import flatten_text as _flatten_text
from executor.usage import merge_usage as _merge_usage
from executor.usage import usage_from_message as _usage_from_message

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Public constants
# --------------------------------------------------------------------------- #
MAX_SUBAGENT_DEPTH = 2
"""Hard structural nesting cap. A Supervisor's structural fan-out consumes one
level just as a ``dispatch_subagent`` call does, so a Supervisor → ReAct-sub →
``dispatch_subagent`` chain reaches the cap. Budget caps bound *cost*; depth
needs its own structural limit so a buggy prompt cannot fork-bomb before the
budget trips."""

SUBAGENT_HEARTBEAT_EVENT = "subagent.heartbeat"
"""Langfuse span-event name. NOT a ``task_events`` CHECK value (S9 must not add
``subagent_heartbeat`` to migration 0025) and NOT a lease touch."""

# Tools that call ``interrupt()`` — sub-agents are headless and must never bind
# these (≥2 pending interrupts in one Send super-step → RuntimeError on the
# worker's scalar ``Command(resume=...)`` path; LangGraph 1.0.5). Extend this
# set when a new pause-bearing tool ships.
INTERRUPT_BEARING_TOOL_NAMES = frozenset({"request_human_input"})

# Tools whose handler returns a LangGraph ``Command`` targeting PARENT-graph
# channels (``plan`` / ``messages`` / ``observations``) and declares
# ``InjectedToolCallId``. Neither works inside the isolated sub-agent subgraph:
# ``_SubagentState`` has none of those channels, and the slim tool loop below
# invokes by bare args (no ToolCall injection) — binding one crashes the
# sub-agent (ValueError from langchain-core's ``_parse_input``). Extend this
# set when a new parent-state-mutating tool ships.
PARENT_STATE_TOOL_NAMES = frozenset(
    {"plan_write", "note_finding", "remember_this_run"}
)

# Retry policy for the sub-agent's per-turn model call, gated on the SAME
# transient classifier the task-level dead-letter decision uses
# (executor/retry_classification.is_retryable_error) — run_subagent converts
# exceptions to failure markers instead of raising, which cuts fan-out
# branches off from the task-level retry path, so the in-place retry must
# apply the identical classification. Custom predicate required: LangGraph's
# ``default_retry_on`` REFUSES the exact failure observed live (task 0729e3a3
# sub-agent 1.3) — botocore's ``ReadTimeoutError`` carries ``OSError`` in its
# MRO, which the default blanket-rejects. Bounded and cheap relative to
# forfeiting the branch: that live failure cost 17 turns of completed
# research (and its findings) to ONE timed-out call.
_SUBAGENT_LLM_RETRY = RetryPolicy(
    retry_on=is_retryable_error,
    max_attempts=3,
    initial_interval=1.0,
)

# Failure reasons (the discriminator on a failed SubagentResult).
REASON_CEILING = "ceiling"
REASON_TIMEOUT = "timeout"
REASON_ERROR = "error"
REASON_DEPTH = "depth"

_DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60  # research timeout ceiling (§A11 decision)

EmitCallable = Callable[..., Awaitable[None]]


def _max_reducer(a: int, b: int) -> int:
    """Monotone watermark reducer for the per-sub-agent counters.

    A stale super-step replayed on resume cannot regress the turn / token
    counts. Mirrors ``executor.compaction.state._max_reducer``. (LangGraph's
    channel introspection cannot read the builtin ``max``'s signature, so a
    named wrapper is required.)"""
    return max(a, b)


def _append(left: list, right: list) -> list:
    """Append reducer for the cross-back ``results`` channel."""
    return list(left) + list(right)


# --------------------------------------------------------------------------- #
# Public shapes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SubagentCeiling:
    """Per-sub-agent token + turn budget, enforced in graph state.

    Either bound trips the ceiling. ``max_turns`` also bounds the emergent
    width of a sub-agent's own ``dispatch_subagent`` fan-out (closing the
    depth-2 *width* question)."""

    max_turns: int
    max_tokens: int


@dataclass(frozen=True)
class SubagentResult:
    """The fan-out return shape — a value, never an exception.

    ``ok=True`` carries the distilled ``summary`` the driver injects. ``ok=False``
    carries a ``reason`` in {``ceiling``, ``timeout``, ``error``, ``depth``}.

    ``usage`` is the sub-agent's ACCUMULATED LLM ``usage_metadata`` (flat token
    fields), surfaced so the caller can attribute the spend to the PARENT task
    (§A11-E1 — the outer ``astream(subgraphs=True)`` cannot see this nested
    ``ainvoke``, so usage must travel back through the return value). A failure
    that never invoked a model (e.g. depth reject) carries ``{}``; a ceiling /
    timeout / mid-run error still surfaces whatever was spent before stopping.
    Pattern A: this is the parent's cost, not a per-sub-agent ledger row.
    """

    ok: bool
    summary: str = ""
    reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    @classmethod
    def success(
        cls, summary: str, *, usage: dict[str, int] | None = None
    ) -> "SubagentResult":
        return cls(ok=True, summary=summary, reason=None, usage=dict(usage or {}))

    @classmethod
    def failure(
        cls, reason: str, *, detail: str = "", usage: dict[str, int] | None = None
    ) -> "SubagentResult":
        return cls(
            ok=False, summary=detail, reason=reason, usage=dict(usage or {})
        )


# --------------------------------------------------------------------------- #
# Headless filter
# --------------------------------------------------------------------------- #
def filter_headless_tools(tools: Iterable[Any]) -> list[Any]:
    """Drop tools a sub-agent can never run from its allowlist.

    Two classes are excluded: ``interrupt()``-bearing tools (sub-agents are
    headless, §A0 inv. 8) and parent-state-channel tools (their ``Command``
    updates target channels absent from ``_SubagentState``).

    Matches by tool ``name`` (the public, stable identifier on
    ``StructuredTool`` / ``BaseTool``). Silently dropped — never bound."""
    excluded = INTERRUPT_BEARING_TOOL_NAMES | PARENT_STATE_TOOL_NAMES
    kept: list[Any] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if name in excluded:
            logger.debug("subagent.headless_filter dropped tool=%s", name)
            continue
        kept.append(tool)
    return kept


# --------------------------------------------------------------------------- #
# Sub-agent state — slim, isolated, on its OWN internal channel
# --------------------------------------------------------------------------- #
class _SubagentState(TypedDict, total=False):
    """Isolated sub-agent working state.

    ``sub_messages`` is the sub-agent's *own* context window — NOT the parent's
    ``messages`` channel — so its working turns never leak into the parent
    context while still being checkpointed under the sub-checkpoint namespace.
    Counters use a monotone ``max`` reducer so a replayed super-step on resume
    cannot regress them (mirrors ``RuntimeState``'s watermark convention)."""

    sub_messages: Annotated[list[BaseMessage], add_messages]
    turn_count: Annotated[int, _max_reducer]
    tokens_used: Annotated[int, _max_reducer]
    # Stable caller-assigned identity (the Supervisor's "<iteration>.<index>"
    # subtask id). Persisted in every sub-checkpoint so read-side projections
    # (the Console's /activity sub-agent tree) can correlate this namespace's
    # transcript with the subtask markers — the namespace itself is an opaque
    # LangGraph task uuid, not the deterministic id the caller knows.
    subtask: str
    # S8 (§A11-E1) — full per-call usage breakdown (input/output/cache tokens)
    # accumulated across the sub-agent's inner turns. Distinct from
    # ``tokens_used`` (a single monotone watermark for the TOKEN ceiling): the
    # cost path needs the per-field breakdown the provider extractor reads, and
    # this is summed (not max'd) so every turn's spend is billed to the parent.
    sub_usage: Annotated[dict, _merge_usage]
    depth: int
    stopped_reason: str
    # The single channel that crosses BACK to the parent. LangGraph maps a
    # subgraph node's output to the parent's same-named channel, so the
    # parent's ``results`` reducer receives this one entry — while
    # ``sub_messages`` (a parent-absent channel) stays isolated.
    results: Annotated[list, _append]


def _message_tokens(msg: BaseMessage) -> int:
    usage = getattr(msg, "usage_metadata", None)
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int):
            return total
        return int(usage.get("input_tokens", 0) or 0) + int(
            usage.get("output_tokens", 0) or 0
        )
    # No usage_metadata (some providers / streaming paths omit it) → 0 tokens
    # counted. The token ceiling then never advances, so the TURN ceiling
    # becomes the binding budget guard for this sub-agent. Intentional: a turn
    # cap always bounds the loop even when a provider reports no token usage.
    return 0


def build_subagent_node(
    *,
    model: Any,
    tools: list[Any],
    ceiling: SubagentCeiling,
    emit: EmitCallable,
):
    """Compile the sub-agent ReAct loop as a subgraph (to be added as a NODE).

    Returns a compiled subgraph with **no own checkpointer** so it inherits the
    parent's (the namespaced sub-checkpoint). The caller wires it as a node and
    reaches it via ``Send`` — *that* is what makes its inner turns checkpointed
    super-steps (per-inner-turn resume + persisted transcript)."""

    headless_tools = filter_headless_tools(tools)
    bound_model = model.bind_tools(headless_tools) if headless_tools else model
    tools_by_name = {t.name: t for t in headless_tools}

    async def agent(state: _SubagentState) -> dict:
        turn = int(state.get("turn_count", 0))
        tokens = int(state.get("tokens_used", 0))

        # Ceiling check BEFORE spending another turn / tokens. On exhaustion,
        # stop the loop and mark the reason — never raise.
        if turn >= ceiling.max_turns or tokens >= ceiling.max_tokens:
            return {"stopped_reason": REASON_CEILING}

        # Wedged-branch liveness: a span event only (never a lease/task_events).
        # Tolerate a missing sink (``emit=None``) — observability must never sink
        # the run (mirrors the None-tolerant helpers in core/subagent_events.py).
        if emit is not None:
            await emit(SUBAGENT_HEARTBEAT_EVENT, {"turn": turn, "tokens": tokens})

        response: AIMessage = await bound_model.ainvoke(state.get("sub_messages", []))
        spent = _message_tokens(response)
        return {
            "sub_messages": [response],
            "turn_count": turn + 1,
            "tokens_used": tokens + spent,
            # S8 (§A11-E1) — accumulate the full per-call usage breakdown so the
            # sub-agent's spend can be billed to the PARENT super-step. Summed
            # via the channel's _merge_usage reducer across every inner turn.
            "sub_usage": _usage_from_message(response),
        }

    async def tool_node(state: _SubagentState) -> dict:
        last = state["sub_messages"][-1]
        out: list[ToolMessage] = []
        for call in getattr(last, "tool_calls", None) or []:
            tool = tools_by_name.get(call["name"])
            if tool is None:
                out.append(
                    ToolMessage(
                        content=f"error: tool {call['name']!r} not available",
                        tool_call_id=call["id"],
                    )
                )
                continue
            # Contain tool failures in-band (prebuilt-ToolNode parity): a
            # raising tool becomes an error ToolMessage the LLM can route
            # around — never an exception that kills the whole sub-agent
            # (and, in round 1 of a fan-out, dead-letters the parent task).
            try:
                result = await tool.ainvoke(call["args"])
            except Exception as exc:  # noqa: BLE001 — any tool error is in-band
                logger.warning(
                    "subagent.tool_error tool=%s: %s", call["name"], exc
                )
                out.append(
                    ToolMessage(
                        content=f"error: {exc}",
                        tool_call_id=call["id"],
                        status="error",
                    )
                )
                continue
            out.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
        return {"sub_messages": out}

    def route(state: _SubagentState) -> str:
        if state.get("stopped_reason"):
            return "finalize"
        last = state["sub_messages"][-1] if state.get("sub_messages") else None
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return "finalize"

    async def finalize(state: _SubagentState) -> dict:
        # Distil the outcome the driver injects and write it to the cross-back
        # ``results`` channel (the ONLY thing that reaches the parent). On a
        # clean stop the summary is the last AI text; on ceiling exhaustion the
        # reason marker carries the meaning and the summary is empty.
        reason = state.get("stopped_reason", "")
        summary = ""
        if not reason:
            for msg in reversed(state.get("sub_messages", [])):
                if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
                    summary = _flatten_text(msg.content)
                    break
        # S8 (§A11-E1) — cross the accumulated usage back on the results channel
        # (the only channel that reaches the parent). On a ceiling/error stop the
        # usage spent before stopping is still billed.
        return {
            "results": [
                {
                    "summary": summary,
                    "stopped_reason": reason,
                    "usage": dict(state.get("sub_usage") or {}),
                }
            ]
        }

    sub = StateGraph(_SubagentState)
    # retry_policy: a transient provider error (timeout / throttle / 5xx) on
    # one turn's model call retries in-place instead of failing the branch —
    # the checkpointed transcript makes the retry cheap; forfeiting it is not.
    sub.add_node("agent", agent, retry_policy=_SUBAGENT_LLM_RETRY)
    sub.add_node("tools", tool_node)
    sub.add_node("finalize", finalize)
    sub.add_edge(START, "agent")
    sub.add_conditional_edges("agent", route, ["tools", "finalize"])
    sub.add_edge("tools", "agent")
    sub.add_edge("finalize", END)
    return sub.compile()  # no own checkpointer -> inherits the parent's


class _ParentFanoutState(TypedDict, total=False):
    """Minimal parent graph state for a single-branch fan-out.

    ``results`` is the bridge channel the sub-agent's outcome lands on — NOT the
    sub-agent's internal ``sub_messages`` — so nothing of the sub-agent's
    working context reaches the parent. (S4/S6 use richer parent states; this
    helper owns only the single-branch driver.)"""

    results: Annotated[list, _append]


# --------------------------------------------------------------------------- #
# run_subagent — the shared driver entry point
# --------------------------------------------------------------------------- #
async def run_subagent(
    prompt: str,
    tools: list[Any],
    *,
    ceiling: SubagentCeiling,
    depth: int,
    model: Any,
    checkpointer: BaseCheckpointSaver,
    thread_id: str,
    emit: EmitCallable,
    checkpoint_ns: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    subagent_node_name: str = "subagent",
    subtask: str | None = None,
) -> SubagentResult:
    """Run one focused ReAct sub-agent in-process and return a structured result.

    The sub-agent is a compiled ReAct subgraph declared as the
    ``subagent_node_name`` node of a tiny parent graph and reached via ``Send``,
    sharing the passed ``checkpointer``. The parent fan-out is driven with
    ``durability="sync"`` (matching the worker runtime) so every inner super-step
    is persisted before the next — the basis of per-inner-turn crash resume.

    ``thread_id`` / ``checkpoint_ns`` durability contract (S8 hardening): the
    repo's ``PostgresDurableCheckpointer`` maps ``thread_id`` → ``task_id::uuid``
    and lease-gates every write against that task row, so a sub-agent sharing the
    parent's checkpointer MUST run on the PARENT's UUID ``thread_id`` — a derived
    non-UUID thread id (the old ``f"{parent}:subagent:{id}"`` form) fails Postgres
    UUID coercion. Pass the parent task UUID as ``thread_id`` and the per-subtask
    isolation key as ``checkpoint_ns`` (LangGraph keys the inner sub-checkpoint by
    namespace under the same task row). When ``checkpoint_ns`` is omitted the
    inner config carries ``thread_id`` only (the MemorySaver unit-test path, which
    does not validate UUIDs).

    Always returns a :class:`SubagentResult`; never raises ceiling / timeout /
    error / depth out to the caller's graph.
    """
    # Depth guard FIRST — reject before any spawn (no model bind, no invoke).
    if depth > MAX_SUBAGENT_DEPTH:
        return SubagentResult.failure(
            REASON_DEPTH,
            detail=f"depth {depth} exceeds MAX_SUBAGENT_DEPTH={MAX_SUBAGENT_DEPTH}",
        )

    try:
        subagent = build_subagent_node(
            model=model, tools=tools, ceiling=ceiling, emit=emit
        )

        # Tiny parent graph: a dispatch node Sends the seeded sub-agent state to
        # the shared subagent NODE. This Send-reached-node wiring (NOT an
        # imperative ainvoke of the subgraph) is the load-bearing durability
        # story — the sub-agent's inner turns become checkpointed super-steps
        # under the ``subagent:<id>`` namespace.
        def _dispatch(_state):
            seed: dict[str, Any] = {
                "sub_messages": [HumanMessage(content=prompt)],
                "turn_count": 0,
                "tokens_used": 0,
                "depth": depth,
            }
            if subtask is not None:
                # Persist the caller's stable subtask id in the sub-checkpoint
                # so read-side projections can correlate transcript ↔ markers.
                seed["subtask"] = subtask
            return [Send(subagent_node_name, seed)]

        # The subagent node writes its outcome to the ``results`` channel, which
        # LangGraph maps to the parent's same-named channel — the only thing
        # that crosses back. Its internal ``sub_messages`` are a parent-absent
        # channel, so they never leak into the parent context.
        parent = StateGraph(_ParentFanoutState)
        parent.add_node("dispatch", lambda s: {})
        parent.add_node(subagent_node_name, subagent)
        parent.add_edge(START, "dispatch")
        parent.add_conditional_edges("dispatch", _dispatch, [subagent_node_name])
        parent.add_edge(subagent_node_name, END)
        compiled = parent.compile(checkpointer=checkpointer)

        # Size the recursion limit so the per-sub-agent TURN ceiling is the
        # binding stop — never LangGraph's default (25). Each turn is ~2 inner
        # super-steps (agent + tools); add headroom for dispatch / finalize.
        recursion_limit = max(25, ceiling.max_turns * 2 + 5)
        _configurable: dict[str, Any] = {"thread_id": thread_id}
        if checkpoint_ns is not None:
            # Isolate this sub-agent's sub-checkpoint under the parent task's
            # thread (UUID) by namespace — see the thread_id/checkpoint_ns note.
            _configurable["checkpoint_ns"] = checkpoint_ns
        config = {
            "configurable": _configurable,
            "recursion_limit": recursion_limit,
        }

        try:
            final = await asyncio.wait_for(
                compiled.ainvoke({}, config=config, durability="sync"),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("subagent.timeout thread_id=%s", thread_id)
            return SubagentResult.failure(
                REASON_TIMEOUT,
                detail=f"sub-agent exceeded its {timeout_seconds:.0f}s research timeout",
            )

        results = final.get("results") or []
        outcome = results[0] if results else {}
        usage = outcome.get("usage") or {}
        if outcome.get("stopped_reason") == REASON_CEILING:
            # A ceiling stop still spent tokens before stopping — bill them.
            return SubagentResult.failure(REASON_CEILING, usage=usage)
        return SubagentResult.success(outcome.get("summary", ""), usage=usage)

    except Exception as exc:  # noqa: BLE001 — failure is a return value, never a raise.
        logger.exception("subagent.unexpected_error thread_id=%s", thread_id)
        # Carry the cause in the marker (typed, truncated) — without it the
        # Console can only say "Failed — error" while the real reason (e.g. a
        # provider read-timeout) is buried in the worker log.
        return SubagentResult.failure(
            REASON_ERROR, detail=f"{type(exc).__name__}: {exc}"[:500]
        )
