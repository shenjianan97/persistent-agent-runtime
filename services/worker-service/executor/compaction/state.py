"""Unified LangGraph state schema for all worker task executions.

``RuntimeState`` is the single TypedDict used by every task graph regardless of
whether the memory stack is enabled or disabled.  Previously the worker
branched on ``stack_enabled`` to choose between two separate TypedDicts;
that branching is gone.  Every graph is now constructed
with ``RuntimeState``, which contains the union of all Track 5 fields plus
``messages``.

Design notes
------------
* **No ``Optional[T]`` on reducer-annotated fields.** LangGraph's channel
  initialisation bypasses the reducer when the seed value is ``None`` (see
  ``langgraph/channels/binop.py`` and the closed-as-by-design issue #4305).
  All fields use direct types and reducer-safe sentinel defaults (``[]``,
  ``{}``, ``False``).
* **``operator.add`` on ``observations``.** The reducer fires only when a
  node's return dict includes the key — unused fields on memory-disabled tasks
  cost nothing at runtime.
* **Track 7 fields are NOT here yet.**  ``cleared_through_turn_index``,
  ``summary_marker``, and other compaction fields will be added in Task 8 of
  Track 7.  At the end of this task (Track 7 Task 2) ``RuntimeState`` contains
  only Track 5 fields plus ``messages``.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages


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
    """

    messages: Annotated[list[BaseMessage], add_messages]

    # Track 5 (memory) fields — populated by memory-enabled graphs only.
    # Defaults are reducer-safe: [] not None (operator.add crashes on None),
    # {} not None, False not None.  Direct types — no Optional[T] — to avoid
    # the reducer-bypass bug described in the module docstring.
    observations: Annotated[list[str], operator.add]
    pending_memory: dict
    memory_opt_in: bool
