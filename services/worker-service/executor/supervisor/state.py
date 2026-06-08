"""Supervisor topology graph state — a strict superset of ``RuntimeState``.

The Supervisor ("Deep Research") topology is a fixed four-phase graph
(Scope → Supervisor+iteration → parallel Subagents → Writer). Every phase runs
on **one** ``thread_id`` / one task / one checkpoint stream (Pattern A —
in-process fan-out; plan §A0 invariant 1), so the Supervisor graph must run the
*same* compaction / checkpoint machinery the ReAct graph runs. That is why
``SupervisorState`` **subclasses** :class:`~executor.compaction.state.RuntimeState`
(``total=False``) rather than redeclaring its channels: it inherits every
``RuntimeState`` channel (``messages`` + the memory / Track-7 compaction fields)
verbatim — same reducers, same defaults — and *adds* the supervisor channels
below. Inheritance (not copy) keeps the two schemas from drifting.

Channel ownership (which node writes each — the contract S6/S7/S8 inherit)
--------------------------------------------------------------------------
* ``brief``            — **scope_node (S5)**, *write-once*. The immutable
  "north star" goal anchor every downstream node reads. Immutability is a
  documented contract, not a runtime lock: no later node returns ``brief``.
* ``iteration``        — initialised to ``0`` by **scope_node (S5)**;
  **supervisor_node (S6)** increments it each fan-out round. Monotone
  (``_max_reducer``) so a replayed super-step on resume cannot regress it.
* ``subtasks``         — initialised to ``[]`` by **scope_node (S5)**;
  **supervisor_node (S6)** writes the parsed ``[{subtask, prompt}]`` plan.
  Last-write-wins (the Supervisor re-emits the whole plan each round).
* ``subagent_results`` — **subagent_node / supervisor merge (S6)** writes;
  S5 only declares the channel + reducer. A checkpointed reducer **keyed by
  ``subtask``** (``_merge_subagent_results``) so partial fan-out results
  survive **idempotently** across crash-resume and operator redrive (design
  decision 2026-06-03(c); plan §A0 invariant 6). S5 leaves it at its empty
  ``{}`` default.
* ``findings``         — **subagent_node / writer_node (S7)** appends the
  accumulated ``{finding_id, claim, source_url, supporting_quote}`` records;
  S5 only declares the channel (``operator.add``). Findings quotes are
  immutable (§A0 invariant 4): the append reducer never rewrites an existing
  entry, and S5 defines no transform over ``supporting_quote``.

Clarify-pause threading
-----------------------
Scope's clarification pause reuses the **existing** ``waiting_for_input``
machinery (the ``interrupt()`` / ``Command(resume=...)`` path at
``tools/definitions.py:449``), which is a *task status* the worker derives from
a pending ``GraphInterrupt`` — **not** a state channel. The clarifying question
travels as the ``interrupt({"type": "input", "prompt": ...})`` payload and the
user's answer comes back as the ``Command(resume=...)`` value; nothing is
threaded through a dedicated state channel. So there is deliberately **no**
``clarification_question`` channel here — adding one would be the parallel pause
state the S5 spec forbids.

Design-note compliance (mirrors ``executor/compaction/state.py``)
-----------------------------------------------------------------
* No ``Optional[T]`` on reducer-annotated fields — reducer-safe sentinel
  defaults (``[]``, ``{}``, ``0``, ``""``).
* Named reducer functions, never builtins: LangGraph 1.0.5 introspects a
  reducer's ``signature`` and raises ``ValueError: no signature found for
  builtin`` on ``max`` / ``dict.update`` (same reason
  ``executor/compaction/state._max_reducer`` and
  ``executor/subagents/fanout._max_reducer`` exist).
"""

from __future__ import annotations

import operator
from typing import Annotated

from executor.compaction.state import RuntimeState, _max_reducer


def _merge_subagent_results(a: dict, b: dict) -> dict:
    """Keyed merge for ``subagent_results`` — keyed by ``subtask``, idempotent.

    Each fan-out branch contributes ``{<subtask>: <result>}``. Merging is a
    plain dict update: a later write for the same ``subtask`` key replaces the
    earlier one (last-write-wins **per key**), and re-delivering an identical
    delta is a no-op. That is exactly the idempotence the design requires
    (decision 2026-06-03(c); plan §A0 invariant 6) — *crash recovery resumes
    forward* (completed siblings are not recomputed) while *operator redrive
    rolls the whole super-step back* (each branch re-writes its own key), and
    either way the channel converges to one entry per ``subtask``.

    A node that does NOT return ``subagent_results`` leaves the prior value
    intact (LangGraph's default for reducer-annotated fields). Default is the
    reducer-safe ``{}`` sentinel, not ``Optional``.

    Named (not ``dict.update`` / a lambda) because LangGraph 1.0.5 introspects
    the reducer's signature and rejects un-introspectable builtins.
    """
    merged = dict(a or {})
    merged.update(b or {})
    return merged


class SupervisorState(RuntimeState, total=False):
    """Graph state for the Supervisor ("Deep Research") topology.

    Strict superset of :class:`~executor.compaction.state.RuntimeState`: every
    inherited channel keeps its original reducer/default so the Supervisor graph
    runs the same compaction + checkpoint machinery as the ReAct graph. The
    supervisor channels below are additive — see the module docstring for the
    per-channel owner contract S6/S7/S8 build on.
    """

    # --- Supervisor channels (additive over RuntimeState) ----------------- #

    # scope_node (S5) writes this once; no later node may write it (write-once
    # contract — the immutable north star). Plain ``str`` (no reducer): a
    # later node returning ``brief`` would simply replace it, which the
    # write-once contract forbids by convention.
    brief: str

    # scope_node (S5) seeds 0; supervisor_node (S6) increments per round.
    # Monotone so a replayed super-step cannot regress the round counter.
    iteration: Annotated[int, _max_reducer]

    # scope_node (S5) seeds []; supervisor_node (S6) writes the parsed
    # [{subtask, prompt}] plan (last-write-wins — full re-emit each round).
    subtasks: list[dict]

    # S6 writes; S5 only declares the channel. Keyed-by-subtask reducer,
    # idempotent across resume/redrive. Empty {} default.
    subagent_results: Annotated[dict, _merge_subagent_results]

    # S7 appends {finding_id, claim, source_url, supporting_quote} records.
    # Append-only (operator.add); quotes are immutable (§A0 inv. 4).
    findings: Annotated[list[dict], operator.add]
