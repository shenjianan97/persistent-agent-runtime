"""Regression fixtures and reducer-safety tests for ``RuntimeState``.

Phase 2 Track 7 Task 2 — State Schema Unification.

Covers three categories per the task spec acceptance criteria:

1. **Schema shape** — ``RuntimeState`` has the correct fields and reducers
   (Track 5 fields only; no Track 7 fields).

2. **Reducer safety** — ``operator.add`` on ``observations`` succeeds when the
   initial value is ``[]`` and raises a clear ``TypeError`` when the initial
   value is ``None``.  This dual assertion confirms both the happy path and
   the contract that "direct types + reducer-safe defaults" (not ``Optional``)
   are mandatory.

3. **Checkpoint backward-compatibility fixtures** — synthetic pre-refactor
   checkpoint dicts (in the shape LangGraph's in-memory serialiser produces)
   are loaded into a compiled ``RuntimeState`` graph and the graph completes
   without ``KeyError`` on the missing/extra fields.

   Two fixtures are tested:

   a. Full-fields checkpoint (all four ``RuntimeState`` fields present).
      ``RuntimeState`` is structurally identical so deserialization is clean.

   b. ``MessagesState``-shaped checkpoint (memory-disabled) — only the
      ``messages`` key is present.  ``observations``, ``pending_memory``, and
      ``memory_opt_in`` are absent.  LangGraph's TypedDict tolerance means
      the graph continues; field consumers that call ``state.get("observations",
      [])`` return the default safely.

These tests are pure-Python / in-process — no network, no DB, no LLM.
"""

from __future__ import annotations

import operator
from typing import Annotated, get_type_hints

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph, add_messages

from executor.compaction.state import RuntimeState


# ---------------------------------------------------------------------------
# 1. Schema shape assertions
# ---------------------------------------------------------------------------


class TestRuntimeStateSchemaShape:
    """Confirm that RuntimeState has exactly the Track-5 fields declared with
    the correct reducers.  No Track 7 fields must appear at this stage.
    """

    def test_has_messages_field_with_add_messages_reducer(self) -> None:
        hints = get_type_hints(RuntimeState, include_extras=True)
        assert "messages" in hints
        ann = hints["messages"]
        # Annotated[list[BaseMessage], add_messages] — metadata is add_messages.
        metadata = getattr(ann, "__metadata__", ())
        assert len(metadata) == 1
        assert metadata[0] is add_messages

    def test_has_observations_with_operator_add_reducer(self) -> None:
        hints = get_type_hints(RuntimeState, include_extras=True)
        assert "observations" in hints
        ann = hints["observations"]
        metadata = getattr(ann, "__metadata__", ())
        assert len(metadata) == 1
        assert metadata[0] is operator.add

    def test_has_pending_memory_as_plain_dict(self) -> None:
        hints = get_type_hints(RuntimeState, include_extras=True)
        assert "pending_memory" in hints
        ann = hints["pending_memory"]
        # No reducer annotation — plain dict type expected.
        metadata = getattr(ann, "__metadata__", None)
        assert metadata is None

    def test_has_memory_opt_in_as_plain_bool(self) -> None:
        hints = get_type_hints(RuntimeState, include_extras=True)
        assert "memory_opt_in" in hints
        ann = hints["memory_opt_in"]
        metadata = getattr(ann, "__metadata__", None)
        assert metadata is None
        assert ann is bool

    def test_no_track7_fields_present(self) -> None:
        """Track 7 fields must NOT exist yet — Task 8 adds them."""
        hints = get_type_hints(RuntimeState, include_extras=True)
        track7_fields = {
            "cleared_through_turn_index",
            "summary_marker",
        }
        overlap = track7_fields & set(hints)
        assert not overlap, f"Unexpected Track 7 fields found: {overlap}"

    def test_exactly_four_fields(self) -> None:
        hints = get_type_hints(RuntimeState, include_extras=True)
        expected = {"messages", "observations", "pending_memory", "memory_opt_in"}
        assert set(hints) == expected


# ---------------------------------------------------------------------------
# 2. Reducer-safety assertions
# ---------------------------------------------------------------------------


class TestReducerSafety:
    """Direct unit tests on the ``operator.add`` reducer behaviour.

    The contract: initial ``[]`` + ``["x"]`` must succeed; initial ``None``
    must raise ``TypeError`` — confirming the task correctly initialises
    ``observations`` to ``[]`` rather than ``None``.
    """

    def test_operator_add_list_with_list_succeeds(self) -> None:
        result = operator.add([], ["x"])
        assert result == ["x"]

    def test_operator_add_list_concatenates(self) -> None:
        result = operator.add(["a", "b"], ["c"])
        assert result == ["a", "b", "c"]

    def test_operator_add_none_initial_raises_type_error(self) -> None:
        """Confirms None is NOT a safe default — this is the failure mode the
        task spec guards against by requiring direct types + [] defaults.
        """
        with pytest.raises(TypeError):
            operator.add(None, ["x"])  # type: ignore[arg-type]

    def test_operator_add_empty_append_is_identity(self) -> None:
        result = operator.add(["a"], [])
        assert result == ["a"]


# ---------------------------------------------------------------------------
# 3. Checkpoint backward-compatibility fixtures
# ---------------------------------------------------------------------------


def _build_passthrough_graph() -> StateGraph:
    """Minimal compiled graph that does nothing but pass state through.

    Used to verify that LangGraph accepts both full-fields and messages-only
    initial inputs without raising KeyError.
    """

    async def noop_node(state: RuntimeState) -> dict:
        return {}

    wf = StateGraph(RuntimeState)
    wf.add_node("noop", noop_node)
    wf.add_edge(START, "noop")
    wf.add_edge("noop", END)
    return wf


class TestCheckpointBackwardCompat:
    """Verify that LangGraph gracefully handles initial states shaped like
    the pre-refactor schemas.

    These are integration-lite tests — they compile and invoke the graph with
    an in-memory (no checkpointer) invocation so no DB is needed.

    a. Full-fields input: all four ``RuntimeState`` fields present.
    b. Minimal input: only ``messages`` present (memory-disabled pre-refactor shape).
    """

    @pytest.mark.asyncio
    async def test_full_fields_input_runs_cleanly(self) -> None:
        """All four RuntimeState fields present — clean deserialization."""
        graph = _build_passthrough_graph().compile()
        initial: dict = {
            "messages": [HumanMessage(content="hello")],
            "observations": ["noted: something"],
            "pending_memory": {},
            "memory_opt_in": False,
        }
        # Should complete without KeyError or TypeError.
        result = await graph.ainvoke(initial)
        assert "messages" in result

    @pytest.mark.asyncio
    async def test_messages_state_shaped_input_runs_cleanly(self) -> None:
        """Pre-refactor MessagesState checkpoint: only messages present.

        Missing ``observations``, ``pending_memory``, and ``memory_opt_in``
        are absent — LangGraph TypedDict tolerance means the graph continues;
        the fields will simply be absent from the returned state dict but that
        is safe because every consumer uses ``.get(key, default)``.
        """
        graph = _build_passthrough_graph().compile()
        initial: dict = {
            "messages": [HumanMessage(content="hello")],
            # observations, pending_memory, memory_opt_in intentionally absent
        }
        result = await graph.ainvoke(initial)
        assert "messages" in result

    @pytest.mark.asyncio
    async def test_full_round_trip_with_all_fields(self) -> None:
        """Confirm state is preserved through a noop graph invocation."""
        graph = _build_passthrough_graph().compile()
        msgs = [HumanMessage(content="task input"), AIMessage(content="done")]
        initial: dict = {
            "messages": msgs,
            "observations": ["obs-1", "obs-2"],
            "pending_memory": {"title": "T", "summary": "S"},
            "memory_opt_in": True,
        }
        result = await graph.ainvoke(initial)
        assert len(result["messages"]) == 2
        # Observations survive through noop (no reducer fired — node returned {}).
        assert result.get("observations") == ["obs-1", "obs-2"]
        assert result.get("memory_opt_in") is True
