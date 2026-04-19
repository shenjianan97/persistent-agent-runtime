"""Unified LangGraph state schema for all worker task executions.

``RuntimeState`` is the single TypedDict used by every task graph regardless of
whether the memory stack is enabled or disabled.  Previously the worker
branched on ``stack_enabled`` to choose between two separate TypedDicts;
that branching is gone.  Every graph is now constructed
with ``RuntimeState``, which contains the union of all Track 5 fields plus
``messages`` and Track 7 (compaction) fields.

Design notes
------------
* **No ``Optional[T]`` on reducer-annotated fields.** LangGraph's channel
  initialisation bypasses the reducer when the seed value is ``None`` (see
  ``langgraph/channels/binop.py`` and the closed-as-by-design issue #4305).
  All fields use direct types and reducer-safe sentinel defaults (``[]``,
  ``{}``, ``False``, ``0``, ``""``).
* **``operator.add`` on ``observations``.** The reducer fires only when a
  node's return dict includes the key — unused fields on memory-disabled tasks
  cost nothing at runtime.
* **Track 7 compaction fields** (added Task 8).  These use custom monotone
  reducers — ``_max_reducer`` for watermarks (integer progress that must never
  regress), ``_any_reducer`` for one-shot boolean flags, and
  ``_summary_marker_strict_append_reducer`` for the KV-cache-safe summary
  accumulator string.  Compaction is always-on; there is no per-agent disable
  knob.
"""

from __future__ import annotations

import logging
import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom reducer functions for Track 7 compaction fields
# ---------------------------------------------------------------------------


def _max_reducer(a: int, b: int) -> int:
    """Monotone watermark reducer — watermarks only advance, never retract.

    A stale super-step that returns a lower watermark value cannot regress the
    channel.  This prevents cache-invalidation if LangGraph replays an old
    super-step during recovery.
    """
    return max(a, b)


def _any_reducer(a: bool, b: bool) -> bool:
    """One-shot monotone reducer for boolean flags.

    Once a flag transitions to ``True`` it stays ``True`` regardless of what
    subsequent nodes return.  Used for ``memory_flush_fired_this_task`` and
    ``tier3_fatal_short_circuited``.
    """
    return a or b


def _summary_marker_strict_append_reducer(
    a: str | None, b: str | None
) -> str | None:
    """Strict-append reducer for the KV-cache-stable summary accumulator.

    Rules
    -----
    * ``b is None`` → return ``a`` unchanged (no update).
    * ``a is None`` → return ``b`` (first write).
    * ``b.startswith(a)`` → return ``b`` (append path — normal second Tier-3).
    * Otherwise → emit ``compaction.summary_marker_non_append`` warning log
      and return ``a`` (REJECT the non-append write).

    The rejection rule enforces Design §Core design rule 1 (KV-cache
    preservation): a non-prefix rewrite would invalidate the cache prefix on
    every subsequent LLM call, undermining the platform's primary cost lever.
    There is no supported replace path in v1; regenerating the marker requires
    a full state reset via ``rollback_last_checkpoint``, which is out of scope.
    """
    if b is None:
        return a
    if a is None:
        return b
    if b.startswith(a):
        return b
    # Non-append write — reject and log.
    logger.warning(
        "compaction.summary_marker_non_append: rejecting non-prefix write; "
        "current_len=%d, incoming_len=%d",
        len(a),
        len(b),
    )
    return a


class RuntimeState(TypedDict):
    """Unified graph state for all worker task executions.

    Fields
    ------
    messages:
        The LangChain message list.  ``add_messages`` reducer merges incoming
        messages associatively (append / update-by-id semantics).

    observations:
        Append-only list of agent observations written by the ``memory_note``
        tool.  Reducer is ``operator.add`` — each
        ``Command(update={"observations": [note]})`` is merged via list
        concatenation.  Memory-disabled tasks never write this field so the
        default ``[]`` is the effective value for their entire lifetime.

    pending_memory:
        Written once by the terminal ``memory_write`` node on memory-enabled
        tasks; read once by the worker's post-astream commit path.  Always a
        plain ``dict`` — the empty sentinel ``{}`` is safe for memory-disabled
        tasks (the commit path checks for presence of expected keys before
        writing).  No reducer — last-write-wins.

    memory_opt_in:
        Task 12 ``agent_decides`` mode flag.  Default ``False``; the
        ``save_memory`` tool sets it to ``True`` via
        ``Command(update={"memory_opt_in": True})``.  No reducer —
        last-write-wins.  The initial-state construction in
        ``GraphExecutor.execute_task`` resets this to ``False`` on every run so
        the opt-in must be re-earned each time.

    cleared_through_turn_index:
        Monotone watermark (``_max_reducer``) tracking the message index up to
        which Tier 1 (tool-result clearing) has already been applied.

    truncated_args_through_turn_index:
        Monotone watermark tracking Tier 1.5 (arg truncation) progress.

    summarized_through_turn_index:
        Monotone watermark tracking Tier 3 (summarization) progress.

    summary_marker:
        Accumulated Tier 3 summary text prepended as a ``SystemMessage`` on
        every LLM call after the first Tier 3 firing.  Reducer is
        ``_summary_marker_strict_append_reducer`` — non-prefix writes are
        rejected to preserve the KV-cache prefix.

    memory_flush_fired_this_task:
        One-shot flag (``_any_reducer``) set True when the pre-Tier-3 memory
        flush fires.  Ensures it fires at most once per task.

    last_super_step_message_count:
        Updated to ``len(raw_messages)`` on every pipeline call.  Used by
        Task 9's heartbeat / progress detection.

    tier3_firings_count:
        Monotone counter of successful Tier 3 firings in this task.  When
        ``>= TIER_3_MAX_FIRINGS_PER_TASK``, Tier 3 is skipped for the rest of
        the task to bound worst-case summarizer cost.

    tier3_fatal_short_circuited:
        One-shot flag (``_any_reducer``) set True when the summarizer reports
        ``skipped_reason='fatal'``.  Prevents re-attempting a fatally-broken
        summarizer on every agent-node call, which would burn per-call cost.
    """

    messages: Annotated[list[BaseMessage], add_messages]

    # Track 5 (memory) fields — populated by memory-enabled graphs only.
    # Defaults are reducer-safe: [] not None (operator.add crashes on None),
    # {} not None, False not None.  Direct types — no Optional[T] — to avoid
    # the reducer-bypass bug described in the module docstring.
    observations: Annotated[list[str], operator.add]
    pending_memory: dict
    memory_opt_in: bool

    # Track 7 (compaction) fields — populated by compact_for_llm on every
    # agent_node call.  Defaults are reducer-safe: 0, "", False.
    # _max_reducer fields never regress; _any_reducer fields are one-shot.
    cleared_through_turn_index: Annotated[int, _max_reducer]
    truncated_args_through_turn_index: Annotated[int, _max_reducer]
    summarized_through_turn_index: Annotated[int, _max_reducer]
    summary_marker: Annotated[str | None, _summary_marker_strict_append_reducer]
    memory_flush_fired_this_task: Annotated[bool, _any_reducer]
    last_super_step_message_count: Annotated[int, _max_reducer]
    tier3_firings_count: Annotated[int, _max_reducer]
    tier3_fatal_short_circuited: Annotated[bool, _any_reducer]
