"""Sub-agent / Supervisor observability event helpers (Agent Modes, Track 2 seam).

This module is the **stable emit-helper interface** the Supervisor topology nodes
call so the worker need not know how the event ultimately reaches storage. It is
promoted to the shared contract (plan §A11 wording note) so S6/S7 can import it
*before* S9 lands — S9 owns the ``task_events`` CHECK-constraint migration
(``0025``) and the ``_insert_task_event`` plumbing, and will wire these helpers
to the real insert at that point.

Deploy-order constraint (§A6): migration ``0025`` (which adds the
``supervisor_iteration`` / ``subagent_*`` CHECK values) must land in production
*before* any worker build that emits these reaches prod — otherwise the
``INSERT INTO task_events`` violates the CHECK. Until S9 wires the insert, the
helpers here are a **structured-log stub** (no DB write), so a build that ships
ahead of the migration cannot violate the constraint. S9 replaces the stub body
with the real ``_insert_task_event`` call (at-least-once, NOT atomic with
checkpoint writes — see plan §A5; per-turn resume re-emits, so
``(event_type, iteration, subtask)`` is the dedup key and the projection is
duplicate-tolerant).

Event-type catalogue (S9 adds the CHECK values for these):

* ``supervisor_iteration`` — ``{iteration, subtasks_emitted, decision, reason}``
  (emitted by S6 ``supervisor_node`` on each loop decision and each cap hit).
* ``subagent_started`` / ``subagent_finding`` / ``subagent_failed`` — emitted by
  the fan-out helper / Writer (S7) carrying ``{iteration, subtask, ...}``.

The Supervisor node receives the concrete sink as an **injected callable**
(``config["configurable"]["supervisor_emit"]``) — the same dependency-injection
convention ``scope_node`` uses for ``scope_model`` (``nodes.py``). That keeps the
node testable (a recording fake in unit tests) and lets the worker bind the real
sink without S6 importing worker internals. The module-level helpers below build
the canonical payload and forward to that callable.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

SUPERVISOR_ITERATION_EVENT = "supervisor_iteration"
"""``task_events.event_type`` value for a Supervisor loop decision / cap hit.
S9 adds this to the migration-``0025`` CHECK constraint."""

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
