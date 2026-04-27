"""Unified LangGraph state schema for all worker task executions.

``RuntimeState`` is the single TypedDict used by every task graph regardless of
whether the memory stack is enabled or disabled.  Previously the worker
branched on ``stack_enabled`` to choose between two separate TypedDicts;
that branching is gone.  Every graph is now constructed
with ``RuntimeState``, which contains the union of all Track 5 fields plus
``messages`` and the Track 7 Follow-up replace-and-rehydrate compaction
fields.

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
* **Track 7 Follow-up (Task 3 replace-and-rehydrate).** The old append-only
  ``summary_marker`` string + ``_summary_marker_strict_append_reducer`` and the
  Tier 1 / 1.5 watermarks (``cleared_through_turn_index``,
  ``truncated_args_through_turn_index``) are GONE. A single ``summary`` field
  (replace semantics) + the monotone ``summarized_through_turn_index``
  watermark now represent everything at indices ``[0 : summarized_through]``.

  Legacy checkpoints written by Track 7 may still contain the dropped fields;
  TypedDict is tolerant of unknown keys on load, and the new hook never reads
  them. The new ``summary`` field starts empty on those checkpoints and is
  populated fresh on the first post-deploy compaction.
"""

from __future__ import annotations

import logging
import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom reducer functions
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


def _summary_replace_reducer(a: str, b: str) -> str:
    """Replace semantics for the Track 7 Follow-up ``summary`` field.

    Whenever the ``pre_model_hook`` emits an update for ``summary``, it is the
    full new summary string (not a delta), so last-write-wins is the correct
    semantics. We still route through a reducer so that nodes that do NOT
    update the summary (i.e. return dicts without the ``summary`` key) leave
    the prior value intact — which is LangGraph's default behaviour for
    annotated fields.

    The reducer is intentionally permissive: the old strict-append check that
    lived on ``summary_marker`` is obsolete — by design the new architecture
    writes a fresh summary each time the trigger fires, which invalidates the
    KV-cache prefix on that one turn. This is the accepted tradeoff for
    producing rich summaries over raw (never-stubbed) middles.
    """
    return b


class RuntimeState(TypedDict, total=False):
    """Unified graph state for all worker task executions.

    Declared with ``total=False`` so TypedDict tolerates the absence of any
    individual field — this is what lets legacy Track 7 checkpoints
    (containing ``summary_marker``/``cleared_through_turn_index``/...) load
    into the new schema without a migration, and also why fresh new-schema
    checkpoints that lack the Track-7-Follow-up ``summary`` field default to
    empty on read.

    Fields
    ------
    messages:
        The LangChain message list.  ``add_messages`` reducer merges incoming
        messages associatively (append / update-by-id semantics).

    observations:
        Append-only list of agent findings written by the ``note_finding``
        tool (formerly ``memory_note``). Reducer is ``operator.add``. Kept
        clean of ``save_memory`` opt-in rationales — those live on their
        own channel (``commit_rationales``) so the memory-detail UI and
        summarizer can render the two concepts separately. Issue #102.

    commit_rationales:
        Append-only list of ``save_memory`` / ``commit_memory`` reasons.
        Each call contributes one entry. Reducer is ``operator.add``.
        Distinct from ``observations`` so the downstream writer and UI
        can treat "why the agent chose to save" as a different field
        from "what the agent learned". Issue #102.

    pending_memory:
        Written once by the terminal ``memory_write`` node on memory-enabled
        tasks.

    memory_opt_in:
        Task 12 ``agent_decides`` mode flag.  Reset to False each run.

    summary:
        Track 7 Follow-up (Task 3) — the single replaceable summary string
        covering all messages at indices ``[0 : summarized_through_turn_index]``.
        Reducer is ``_summary_replace_reducer`` (last-write-wins). Defaults to
        empty string; empty means "nothing summarised yet" and the projection
        omits the summary ``SystemMessage`` entirely.

    summarized_through_turn_index:
        Monotone watermark (``_max_reducer``) tracking summarisation progress.
        Messages at indices ``[0 : summarized_through_turn_index]`` are
        represented by ``summary`` and no longer appear in the projection's
        middle region.

    memory_flush_fired_this_task:
        One-shot flag (``_any_reducer``) set True when the pre-summarisation
        memory flush fires.  Ensures it fires at most once per task.

    last_super_step_message_count:
        Updated to ``len(state["messages"])`` on every pipeline call.  Used by
        heartbeat / progress detection.

    tier3_firings_count:
        Monotone counter of successful summarisation firings in this task.

    tier3_fatal_short_circuited:
        One-shot flag (``_any_reducer``) set True when the summariser reports
        ``skipped_reason='fatal'``.  Prevents re-attempting a fatally-broken
        summariser on every agent-node call.
    """

    messages: Annotated[list[BaseMessage], add_messages]

    # Track 5 (memory) fields — populated by memory-enabled graphs only.
    observations: Annotated[list[str], operator.add]
    # Issue #102 — save_memory/commit_memory opt-in rationales. Lives
    # alongside ``observations`` with the same ``operator.add`` reducer.
    commit_rationales: Annotated[list[str], operator.add]
    pending_memory: dict
    memory_opt_in: bool

    # Track 7 Follow-up (replace-and-rehydrate) fields.
    # Defaults are reducer-safe: 0, "", False.
    summary: Annotated[str, _summary_replace_reducer]
    summarized_through_turn_index: Annotated[int, _max_reducer]
    memory_flush_fired_this_task: Annotated[bool, _any_reducer]
    last_super_step_message_count: Annotated[int, _max_reducer]
    tier3_firings_count: Annotated[int, _max_reducer]
    tier3_fatal_short_circuited: Annotated[bool, _any_reducer]
