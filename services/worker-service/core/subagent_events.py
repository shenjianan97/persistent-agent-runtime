"""Sub-agent / Supervisor observability event helpers (Agent Modes, Track 2 seam).

This module is the **stable emit-helper interface** the Supervisor topology nodes
call so the worker need not know how the event ultimately reaches storage. S9
owns the ``task_events`` CHECK-constraint migration (``0025``) and the real sink
plumbing: :func:`build_task_event_sink` returns the concrete ``emit`` callable
the worker injects (``config["configurable"]["supervisor_emit"]``), which writes
real ``task_events`` rows via ``_insert_task_event``.

Deploy-order constraint (§A6): migration ``0025`` (which adds the
``supervisor_iteration`` / ``subagent_*`` CHECK values) MUST land in production
*before* any worker build that emits these reaches prod — otherwise the
``INSERT INTO task_events`` violates the CHECK. When ``emit`` is ``None`` (a node
invoked without a configured sink — e.g. a unit test) the helpers degrade to a
structured log so the marker is still observable and never raises into the graph.

At-least-once contract (load-bearing, §A5; NOT atomic with checkpoint writes):
the LangGraph checkpointer (``PostgresDurableCheckpointer.aput`` /
``aput_writes``) opens its OWN connections and transactions, so an event emitted
during node execution can NEVER share them. :func:`build_task_event_sink`
acquires its own short-lived connection from the pool per emit — markers can
commit without their checkpointed state (or vice versa), and a crashed-and-
resumed inner step re-emits. Every event therefore carries the stable dedup key
``(event_type, iteration, subtask)`` (in ``details``); the projection / any
consumer dedups on it (first-wins for ``subagent_started``; last-wins for
``subagent_failed`` / result-bearing markers).

Event-type catalogue (admitted by migration ``0025``):

* ``supervisor_iteration`` — ``{iteration, subtasks_emitted, decision, reason}``
  (emitted by S6 ``supervisor_node`` on each loop decision and each cap hit).
* ``subagent_started`` / ``subagent_finding`` / ``subagent_failed`` — emitted by
  S6's fan-out node / Writer (S7) carrying ``{iteration, subtask, ...}``.

``subagent_heartbeat`` is DELIBERATELY NOT in this catalogue — it is a Langfuse
span event, never a ``task_events`` row (§A11-E4 /
``executor/subagents/fanout.py``).

The Supervisor node receives the concrete sink as an **injected callable**
(``config["configurable"]["supervisor_emit"]``) — the same dependency-injection
convention ``scope_node`` uses for ``scope_model`` (``nodes.py``). That keeps the
node testable (a recording fake in unit tests) and lets the worker bind the real
sink without S6 importing worker internals. The module-level helpers below build
the canonical payload and forward to that callable.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

SUPERVISOR_ITERATION_EVENT = "supervisor_iteration"
"""``task_events.event_type`` value for a Supervisor loop decision / cap hit.
Admitted by migration ``0025``."""

SUBAGENT_STARTED_EVENT = "subagent_started"
"""``task_events.event_type`` value for a sub-agent dispatched in a fan-out
super-step (S6). Admitted by migration ``0025``."""

SUBAGENT_FINDING_EVENT = "subagent_finding"
"""``task_events.event_type`` value for a single structured finding emitted by a
sub-agent (Task S7). Admitted by migration ``0025``."""

SUBAGENT_FAILED_EVENT = "subagent_failed"
"""``task_events.event_type`` value for a sub-agent that exhausted its ceiling /
timeout or errored (S6). Admitted by migration ``0025``."""

SINK_ADMITTED_EVENT_TYPES = frozenset({
    SUPERVISOR_ITERATION_EVENT,
    SUBAGENT_STARTED_EVENT,
    SUBAGENT_FINDING_EVENT,
    SUBAGENT_FAILED_EVENT,
})
"""The ONLY event_type values :func:`build_task_event_sink` writes to
``task_events`` — exactly migration ``0025``'s four additions. The SAME injected
``emit`` sink is also used by ``build_subagent_node`` for the
``subagent.heartbeat`` SPAN event (``executor/subagents/fanout.py``); that
heartbeat is a Langfuse span event and is DELIBERATELY NOT a ``task_events`` row
(§A11-E4), so the sink drops any event_type outside this set rather than
attempting an INSERT that the CHECK constraint would reject."""

PROMPT_PREVIEW_MAX_CHARS = 200
"""Cap on the ``prompt_preview`` carried in a ``subagent_started`` row's
``details`` — bounds row size on a wide fan-out (the full prompt rides the
Langfuse span / sub-checkpoint, not the marker)."""

# The injected sink shape: ``await emit(event_type, details)``. Deliberately
# NARROWER than the fan-out helper's ``Callable[..., Awaitable[None]]``
# (``executor/subagents/fanout.py``): every call here is exactly
# ``emit(event_type, details)`` with a non-``None`` ``dict`` payload, so the
# precise two-positional-arg signature documents the supervisor-event contract.
# A concrete sink (e.g. the worker's ``task_events`` writer) satisfies both the
# broad fan-out type and this narrowed one.
EmitCallable = Callable[[str, dict], Awaitable[None]]


async def emit_supervisor_iteration(
    emit: EmitCallable | None,
    *,
    iteration: int,
    subtasks_emitted: int,
    decision: str,
    reason: str = "",
) -> None:
    """Emit a ``supervisor_iteration`` event with the canonical payload.

    Payload (plan §A7): ``{iteration, subtasks_emitted, decision, reason}``.
    ``reason`` records the cap reason on a clamp/iteration-cap hit and is empty
    on an ordinary decision. ``emit`` is the injected sink; when ``None`` (a
    node invoked without a configured sink) this degrades to a structured log
    so the decision is still observable and never raises into the graph.
    """
    details = {
        "iteration": iteration,
        "subtasks_emitted": subtasks_emitted,
        "decision": decision,
        "reason": reason,
    }
    if emit is None:
        logger.info("%s %s", SUPERVISOR_ITERATION_EVENT, details)
        return
    try:
        await emit(SUPERVISOR_ITERATION_EVENT, details)
    except Exception:  # noqa: BLE001 — observability must never sink the run.
        logger.exception(
            "%s emit failed (non-fatal) details=%s",
            SUPERVISOR_ITERATION_EVENT,
            details,
        )


async def emit_subagent_finding(
    emit: EmitCallable | None,
    *,
    iteration: int,
    subtask: str,
    finding_id: str,
    source_url: str,
) -> None:
    """Emit a ``subagent_finding`` event with the canonical payload.

    Payload (plan §A7): ``{iteration, subtask, finding_id, source_url}``. The
    ``claim`` and ``supporting_quote`` are DELIBERATELY excluded from the row —
    they ride the Langfuse span instead, to bound the ``task_events`` row size on
    a wide fan-out × many-findings run (§A7). ``emit`` is the injected sink; when
    ``None`` this degrades to a structured log so the finding is still observable
    and never raises into the graph.

    Stub-forwarding-to-injected-sink, identical to
    :func:`emit_supervisor_iteration`. S9 owns the migration-``0025`` CHECK value
    + the real ``_insert_task_event`` body and the projection; S7 only calls this
    helper.
    """
    details = {
        "iteration": iteration,
        "subtask": subtask,
        "finding_id": finding_id,
        "source_url": source_url,
    }
    if emit is None:
        logger.info("%s %s", SUBAGENT_FINDING_EVENT, details)
        return
    try:
        await emit(SUBAGENT_FINDING_EVENT, details)
    except Exception:  # noqa: BLE001 — observability must never sink the run.
        logger.exception(
            "%s emit failed (non-fatal) details=%s",
            SUBAGENT_FINDING_EVENT,
            details,
        )


async def emit_subagent_started(
    emit: EmitCallable | None,
    *,
    iteration: int,
    subtask: str,
    prompt_preview: str,
    tool_allowlist: list,
    depth: int,
) -> None:
    """Emit a ``subagent_started`` event with the canonical payload.

    Payload (plan §A7): ``{iteration, subtask, prompt_preview, tool_allowlist,
    depth}``. ``prompt_preview`` is TRUNCATED to
    :data:`PROMPT_PREVIEW_MAX_CHARS` so the row stays small on a wide fan-out
    (the full prompt rides the Langfuse span / sub-checkpoint). ``emit`` is the
    injected sink; when ``None`` this degrades to a structured log so the
    dispatch is still observable and never raises into the graph.

    Lifecycle telemetry (the projection maps it detail-only); the
    ``(event_type, iteration, subtask)`` dedup key makes a per-turn-resume
    re-emit idempotent for consumers (first-wins).
    """
    preview = str(prompt_preview or "")[:PROMPT_PREVIEW_MAX_CHARS]
    details = {
        "iteration": iteration,
        "subtask": subtask,
        "prompt_preview": preview,
        "tool_allowlist": list(tool_allowlist or []),
        "depth": depth,
    }
    if emit is None:
        logger.info("%s %s", SUBAGENT_STARTED_EVENT, details)
        return
    try:
        await emit(SUBAGENT_STARTED_EVENT, details)
    except Exception:  # noqa: BLE001 — observability must never sink the run.
        logger.exception(
            "%s emit failed (non-fatal) details=%s",
            SUBAGENT_STARTED_EVENT,
            details,
        )


async def emit_subagent_failed(
    emit: EmitCallable | None,
    *,
    iteration: int,
    subtask: str,
    reason: str,
) -> None:
    """Emit a ``subagent_failed`` event with the canonical payload.

    Payload (plan §A7): ``{iteration, subtask, reason}`` where ``reason ∈
    {ceiling, timeout, error}`` (the discriminator on a failed
    ``SubagentResult``; ``depth`` rejects are a structural guard the Supervisor
    fan-out never trips, so they collapse to ``error`` at the call site if they
    ever surface). ``emit`` is the injected sink; when ``None`` this degrades to
    a structured log so the failure is still observable and never raises into the
    graph.

    User-meaningful (a customer should see a sub-agent failed even on the coarse
    view). The ``(event_type, iteration, subtask)`` dedup key makes a
    per-turn-resume re-emit idempotent for consumers (last-wins).
    """
    details = {
        "iteration": iteration,
        "subtask": subtask,
        "reason": reason,
    }
    if emit is None:
        logger.info("%s %s", SUBAGENT_FAILED_EVENT, details)
        return
    try:
        await emit(SUBAGENT_FAILED_EVENT, details)
    except Exception:  # noqa: BLE001 — observability must never sink the run.
        logger.exception(
            "%s emit failed (non-fatal) details=%s",
            SUBAGENT_FAILED_EVENT,
            details,
        )


def build_task_event_sink(
    pool: Any,
    *,
    task_id: str,
    tenant_id: str,
    agent_id: str,
) -> EmitCallable:
    """Build the REAL ``emit(event_type, details)`` sink for the worker (S9).

    Returns an ``async`` callable matching :data:`EmitCallable` that, per emit,
    acquires its OWN short-lived connection from ``pool`` and writes one
    ``task_events`` row via ``_insert_task_event`` with ``status_before`` /
    ``status_after`` both ``None`` (these are activity markers, not state
    transitions). ``iteration`` / ``subtask`` ride in ``details`` (Pattern A — no
    columns).

    At-least-once, NOT atomic with the checkpoint write (§A5): the checkpointer
    owns its own connections/transactions (``checkpointer/postgres.py``), so this
    sink can never share them — it deliberately acquires a separate connection.
    A crashed-and-resumed inner step re-emits; the ``(event_type, iteration,
    subtask)`` dedup key (in ``details``) is what consumers group/dedup on. The
    emit is best-effort: a failure is logged, never raised into the graph
    (the ``emit_*`` helpers also wrap their call, but the sink guards directly so
    a non-helper caller is equally safe).

    ``pool`` is the worker's asyncpg pool (``self.pool`` in ``executor/graph``);
    the import of ``_insert_task_event`` is deferred to call time to avoid a
    module-level import cycle with ``core/reaper``.
    """

    async def _emit(event_type: str, details: dict | None = None) -> None:
        # The same injected sink also carries the subagent.heartbeat SPAN event
        # (build_subagent_node). That is a Langfuse span event, NOT a task_events
        # row (§A11-E4) — drop anything outside the admitted set so it never hits
        # the CHECK constraint.
        if event_type not in SINK_ADMITTED_EVENT_TYPES:
            return

        from core.reaper import _insert_task_event  # deferred — avoid import cycle

        try:
            async with pool.acquire() as conn:
                await _insert_task_event(
                    conn,
                    task_id,
                    tenant_id,
                    agent_id,
                    event_type,
                    None,  # status_before
                    None,  # status_after
                    details=details or {},
                )
        except Exception:  # noqa: BLE001 — observability must never sink the run.
            logger.exception(
                "task_event sink insert failed (non-fatal) task_id=%s event=%s",
                task_id,
                event_type,
            )

    return _emit
