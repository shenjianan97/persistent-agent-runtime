"""Unit tests for Track 7 RuntimeState reducers.

Tests are RED-first (TDD) — they assert the contracts that must hold for the
reducer functions defined in executor.compaction.state (Task 8 additions).

Covers:
1. _max_reducer — monotone watermark (integers only ever advance)
2. _any_reducer — one-shot boolean flag (once True, stays True)
3. _summary_marker_strict_append_reducer — appends when b starts with a;
   rejects non-append writes; handles None edge cases
4. RuntimeState TypedDict — confirms Track 7 fields are present with
   correct Annotated metadata; overwrites Track 2 "no track7 fields" assertion.
5. Backward-compat: existing Track 5 checkpoint (no Track 7 keys) loads OK.
"""

from __future__ import annotations

import logging
from typing import get_type_hints

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from executor.compaction.state import (
    RuntimeState,
    _any_reducer,
    _max_reducer,
    _summary_marker_strict_append_reducer,
)


# ---------------------------------------------------------------------------
# 1. _max_reducer
# ---------------------------------------------------------------------------


class TestMaxReducer:
    def test_max_reducer_returns_larger_value(self):
        assert _max_reducer(3, 10) == 10

    def test_max_reducer_returns_current_when_b_is_lower(self):
        """Stale super-step must NOT regress watermark."""
        assert _max_reducer(10, 0) == 10

    def test_max_reducer_identity_on_equal(self):
        assert _max_reducer(5, 5) == 5

    def test_max_reducer_zero_base(self):
        assert _max_reducer(0, 7) == 7

    def test_max_reducer_stale_does_not_regress(self):
        """Simulate a stale super-step returning cleared_through_turn_index=0
        when actual state is 10 — must return 10."""
        current = 10
        stale = 0
        assert _max_reducer(current, stale) == current


# ---------------------------------------------------------------------------
# 2. _any_reducer
# ---------------------------------------------------------------------------


class TestAnyReducer:
    def test_any_reducer_false_false(self):
        assert _any_reducer(False, False) is False

    def test_any_reducer_true_false(self):
        assert _any_reducer(True, False) is True

    def test_any_reducer_false_true(self):
        assert _any_reducer(False, True) is True

    def test_any_reducer_true_true(self):
        assert _any_reducer(True, True) is True

    def test_any_reducer_one_shot_once_true_stays_true(self):
        """Flag is monotone — once fired it never goes back to False."""
        result = _any_reducer(True, False)
        assert result is True


# ---------------------------------------------------------------------------
# 3. _summary_marker_strict_append_reducer
# ---------------------------------------------------------------------------


class TestSummaryMarkerStrictAppendReducer:
    def test_b_none_returns_a(self):
        """No update: return current value unchanged."""
        assert _summary_marker_strict_append_reducer("summary-v1", None) == "summary-v1"

    def test_a_none_returns_b(self):
        """First write: b becomes the marker."""
        assert _summary_marker_strict_append_reducer(None, "summary-v1") == "summary-v1"

    def test_append_case_b_starts_with_a(self):
        """Normal second Tier-3 path: b extends a."""
        a = "Summary of steps 0-5.\n"
        b = "Summary of steps 0-5.\nSummary of steps 6-10.\n"
        result = _summary_marker_strict_append_reducer(a, b)
        assert result == b

    def test_non_append_rejected_returns_a(self, caplog):
        """Non-append write MUST be rejected and a must be returned."""
        a = "Summary of steps 0-5.\n"
        b = "Completely different summary."  # does NOT start with a
        with caplog.at_level(logging.WARNING, logger="executor.compaction.state"):
            result = _summary_marker_strict_append_reducer(a, b)
        assert result == a, "Original marker must be preserved on non-append write"

    def test_non_append_logs_structured_event(self, caplog):
        """Confirm compaction.summary_marker_non_append is logged on rejection."""
        a = "Summary of steps 0-5.\n"
        b = "Completely different summary."
        with caplog.at_level(logging.WARNING):
            _summary_marker_strict_append_reducer(a, b)
        # The log event must reference the marker event name
        assert any(
            "summary_marker_non_append" in record.message
            for record in caplog.records
        ), "Expected compaction.summary_marker_non_append log on non-append write"

    def test_empty_string_a_accepts_any_b(self):
        """Empty string a: every b starts with "", so it's an append."""
        result = _summary_marker_strict_append_reducer("", "first summary")
        assert result == "first summary"

    def test_both_none_returns_none(self):
        """Edge case: both None — b is None so return a (None)."""
        assert _summary_marker_strict_append_reducer(None, None) is None

    def test_exact_match_is_append(self):
        """b == a is technically a valid append (b starts with a)."""
        a = "summary"
        b = "summary"
        assert _summary_marker_strict_append_reducer(a, b) == b


# ---------------------------------------------------------------------------
# 4. RuntimeState schema — Track 7 fields
# ---------------------------------------------------------------------------


class TestRuntimeStateTrack7Fields:
    """Verify Track 7 fields are present with correct Annotated metadata."""

    def _hints(self):
        return get_type_hints(RuntimeState, include_extras=True)

    def test_cleared_through_turn_index_has_max_reducer(self):
        hints = self._hints()
        assert "cleared_through_turn_index" in hints
        ann = hints["cleared_through_turn_index"]
        meta = getattr(ann, "__metadata__", ())
        assert len(meta) == 1
        assert meta[0] is _max_reducer

    def test_truncated_args_through_turn_index_has_max_reducer(self):
        hints = self._hints()
        ann = hints["truncated_args_through_turn_index"]
        meta = getattr(ann, "__metadata__", ())
        assert meta[0] is _max_reducer

    def test_summarized_through_turn_index_has_max_reducer(self):
        hints = self._hints()
        ann = hints["summarized_through_turn_index"]
        meta = getattr(ann, "__metadata__", ())
        assert meta[0] is _max_reducer

    def test_summary_marker_has_strict_append_reducer(self):
        hints = self._hints()
        ann = hints["summary_marker"]
        meta = getattr(ann, "__metadata__", ())
        assert meta[0] is _summary_marker_strict_append_reducer

    def test_memory_flush_fired_this_task_has_any_reducer(self):
        hints = self._hints()
        ann = hints["memory_flush_fired_this_task"]
        meta = getattr(ann, "__metadata__", ())
        assert meta[0] is _any_reducer

    def test_last_super_step_message_count_has_max_reducer(self):
        hints = self._hints()
        ann = hints["last_super_step_message_count"]
        meta = getattr(ann, "__metadata__", ())
        assert meta[0] is _max_reducer

    def test_tier3_firings_count_has_max_reducer(self):
        hints = self._hints()
        ann = hints["tier3_firings_count"]
        meta = getattr(ann, "__metadata__", ())
        assert meta[0] is _max_reducer

    def test_tier3_fatal_short_circuited_has_any_reducer(self):
        hints = self._hints()
        ann = hints["tier3_fatal_short_circuited"]
        meta = getattr(ann, "__metadata__", ())
        assert meta[0] is _any_reducer

    def test_track5_fields_unchanged(self):
        """Track 5 fields must still be present."""
        hints = self._hints()
        for field in ("messages", "observations", "pending_memory", "memory_opt_in"):
            assert field in hints, f"Track 5 field {field!r} missing"

    def test_total_field_count(self):
        """12 total fields: 4 Track 5 + 8 Track 7."""
        hints = self._hints()
        expected = {
            # Track 5
            "messages", "observations", "pending_memory", "memory_opt_in",
            # Track 7
            "cleared_through_turn_index", "truncated_args_through_turn_index",
            "summarized_through_turn_index", "summary_marker",
            "memory_flush_fired_this_task", "last_super_step_message_count",
            "tier3_firings_count", "tier3_fatal_short_circuited",
        }
        assert set(hints) == expected


# ---------------------------------------------------------------------------
# 5. Backward-compat: Track-5-only checkpoint loads into Track-7 graph
# ---------------------------------------------------------------------------


def _build_passthrough_graph() -> StateGraph:
    """Minimal noop graph for checkpoint compatibility tests."""

    async def noop_node(state: RuntimeState) -> dict:
        return {}

    wf = StateGraph(RuntimeState)
    wf.add_node("noop", noop_node)
    wf.add_edge(START, "noop")
    wf.add_edge("noop", END)
    return wf


class TestCheckpointBackwardCompat:
    @pytest.mark.asyncio
    async def test_track5_checkpoint_loads_cleanly(self):
        """Pre-Task-8 checkpoint: only Track 5 fields present.

        Track 7 fields are absent — LangGraph tolerates this; field consumers
        must use .get(key, default) so no KeyError is raised.
        """
        graph = _build_passthrough_graph().compile()
        initial: dict = {
            "messages": [HumanMessage(content="hello")],
            "observations": [],
            "pending_memory": {},
            "memory_opt_in": False,
            # Track 7 fields intentionally absent
        }
        result = await graph.ainvoke(initial)
        assert "messages" in result

    @pytest.mark.asyncio
    async def test_full_track7_state_round_trips(self):
        """All 12 fields present — graph completes and state is preserved."""
        graph = _build_passthrough_graph().compile()
        initial: dict = {
            "messages": [HumanMessage(content="hi"), AIMessage(content="bye")],
            "observations": ["obs-1"],
            "pending_memory": {},
            "memory_opt_in": False,
            "cleared_through_turn_index": 3,
            "truncated_args_through_turn_index": 2,
            "summarized_through_turn_index": 1,
            "summary_marker": "Summary of steps 0-1.\n",
            "memory_flush_fired_this_task": True,
            "last_super_step_message_count": 4,
            "tier3_firings_count": 1,
            "tier3_fatal_short_circuited": False,
        }
        result = await graph.ainvoke(initial)
        assert len(result["messages"]) == 2
        assert result.get("cleared_through_turn_index") == 3

    @pytest.mark.asyncio
    async def test_stale_watermark_does_not_regress_in_graph(self):
        """When a node returns a lower watermark, _max_reducer prevents regression.

        Simulate a stale super-step that tries to write cleared_through_turn_index=0
        while state is at 5 — the reducer must keep 5.
        """

        async def stale_node(state: RuntimeState) -> dict:
            # Stale super-step: returns 0 which is less than the current 5
            return {"cleared_through_turn_index": 0}

        wf = StateGraph(RuntimeState)
        wf.add_node("stale", stale_node)
        wf.add_edge(START, "stale")
        wf.add_edge("stale", END)
        graph = wf.compile()

        initial = {
            "messages": [HumanMessage(content="test")],
            "cleared_through_turn_index": 5,
        }
        result = await graph.ainvoke(initial)
        # _max_reducer must return max(5, 0) = 5
        assert result["cleared_through_turn_index"] == 5
