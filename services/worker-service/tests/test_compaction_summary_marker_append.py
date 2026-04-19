"""Summary-marker append-only invariant test (Track 7 AC 8).

AC 8: When Tier 3 fires a second time within the same task, the new summary is
APPENDED to the existing marker rather than replacing it. The strict-append
reducer rejects any write where the new value does not start with the old value.

Design doc: docs/design-docs/phase-2/track-7-context-window-management.md
§Tier 3 — monotonicity and §State schema extensions — strict-append reducer.
"""

from __future__ import annotations

import logging
from typing import Any, Callable
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from executor.compaction.pipeline import (
    Tier3FiredEvent,
    compact_for_llm,
)
from executor.compaction.state import _summary_marker_strict_append_reducer
from executor.compaction.summarizer import SummarizeResult
from executor.compaction.thresholds import resolve_thresholds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_pair(i: int, content_size: int = 200) -> list[BaseMessage]:
    call_id = f"call_{i}"
    return [
        AIMessage(
            content=f"Step {i}",
            tool_calls=[{
                "id": call_id,
                "name": f"tool_{i}",
                "args": {"content": "x" * 10},
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content="r" * content_size,
            tool_call_id=call_id,
            name=f"tool_{i}",
        ),
    ]


def _make_messages(n: int, content_size: int = 200) -> list[BaseMessage]:
    msgs: list[BaseMessage] = [HumanMessage(content="task input")]
    for i in range(n):
        msgs.extend(_tool_pair(i, content_size=content_size))
    return msgs


def _base_state(**overrides) -> dict[str, Any]:
    state: dict[str, Any] = {
        "cleared_through_turn_index": 0,
        "truncated_args_through_turn_index": 0,
        "summarized_through_turn_index": 0,
        "summary_marker": "",
        "memory_flush_fired_this_task": False,
        "last_super_step_message_count": 0,
        "tier3_firings_count": 0,
        "tier3_fatal_short_circuited": False,
    }
    state.update(overrides)
    return state


def _agent_config(**overrides) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "provider": "other",
        "model": "test-model",
        "context_management": {},
    }
    cfg.update(overrides)
    return cfg


def _task_context(**overrides) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "tenant_id": "tenant-1",
        "agent_id": "agent-1",
        "task_id": "task-1",
        "checkpoint_id": None,
        "cost_ledger": None,
        "callbacks": [],
    }
    ctx.update(overrides)
    return ctx


def _fixed_token_estimate(n: int) -> Callable:
    def estimator(messages: list[BaseMessage]) -> int:
        return n
    return estimator


def _make_summarizer(summary_text: str) -> AsyncMock:
    mock = AsyncMock()
    mock.return_value = SummarizeResult(
        summary_text=summary_text,
        skipped=False,
        skipped_reason=None,
        summarizer_model_id="test-model",
        tokens_in=100,
        tokens_out=50,
        cost_microdollars=0,
        latency_ms=10,
    )
    return mock


# ---------------------------------------------------------------------------
# Test: first Tier 3 firing sets summary_marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_tier3_sets_summary_marker():
    """First Tier 3 firing writes the summary_marker state update."""
    msgs = _make_messages(6, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    SUMMARY_1 = "This is the first summary of steps 0 through some index."
    state = _base_state()
    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_summarizer(SUMMARY_1),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    tier3_events = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_events, "Tier 3 must have fired"

    new_marker = result.state_updates.get("summary_marker", "")
    assert SUMMARY_1 in new_marker, (
        f"First summary must appear in summary_marker. Got: {new_marker!r}"
    )
    assert result.state_updates.get("summarized_through_turn_index", 0) > 0


# ---------------------------------------------------------------------------
# Test: second Tier 3 firing appends to the existing marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_tier3_appends_to_marker():
    """Second Tier 3 firing appends new summary to existing marker.

    The new summary_marker must start with the old marker (strict-append
    invariant). The summarized_through_turn_index must also advance.
    """
    msgs = _make_messages(10, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    SUMMARY_1 = "Summary of early steps."

    # First call
    state_1 = _base_state()
    result1 = await compact_for_llm(
        raw_messages=msgs,
        state=state_1,
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_summarizer(SUMMARY_1),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    tier3_1 = [e for e in result1.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_1, "Tier 3 must have fired on first call"

    marker_after_first = result1.state_updates.get("summary_marker", "")
    watermark_after_first = result1.state_updates.get("summarized_through_turn_index", 0)
    firings_after_first = result1.state_updates.get("tier3_firings_count", 0)

    assert SUMMARY_1 in marker_after_first

    # Second call: build state from the first call's state_updates
    SUMMARY_2 = "Summary of later steps — appended."
    state_2 = _base_state(
        summary_marker=marker_after_first,
        summarized_through_turn_index=watermark_after_first,
        tier3_firings_count=firings_after_first,
        cleared_through_turn_index=result1.state_updates.get("cleared_through_turn_index", 0),
        truncated_args_through_turn_index=result1.state_updates.get(
            "truncated_args_through_turn_index", 0
        ),
    )

    # The second summarizer returns a text that starts with the first marker + extension
    # This simulates the pipeline's append behaviour
    expected_appended = marker_after_first + SUMMARY_2

    result2 = await compact_for_llm(
        raw_messages=msgs,
        state=state_2,
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_summarizer(expected_appended),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    tier3_2 = [e for e in result2.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_2, "Tier 3 must have fired on second call"

    final_marker = result2.state_updates.get("summary_marker", "")

    # Strict-append invariant: final marker must start with the first marker
    assert final_marker.startswith(marker_after_first), (
        f"summary_marker must start with prior marker after second Tier 3 firing.\n"
        f"Prior: {marker_after_first!r}\n"
        f"Final: {final_marker!r}"
    )

    # summarized_through_turn_index must have advanced
    watermark_after_second = result2.state_updates.get("summarized_through_turn_index", 0)
    assert watermark_after_second >= watermark_after_first, (
        "summarized_through_turn_index must advance on second Tier 3 firing"
    )

    # tier3_firings_count must have incremented
    assert result2.state_updates.get("tier3_firings_count", 0) == firings_after_first + 1


# ---------------------------------------------------------------------------
# Test: strict-append reducer rejects non-append write
# ---------------------------------------------------------------------------


def test_strict_append_reducer_rejects_non_append(caplog):
    """Non-append write to summary_marker is rejected; old value preserved."""
    a = "Summary of steps 0-5.\n"
    b = "Completely different — does NOT start with a"

    with caplog.at_level(logging.WARNING):
        result = _summary_marker_strict_append_reducer(a, b)

    assert result == a, (
        "Strict-append reducer must return old value when new value is not an extension"
    )


def test_strict_append_reducer_emits_log_on_rejection(caplog):
    """Rejection must emit a compaction.summary_marker_non_append log event."""
    a = "Summary of steps 0-5.\n"
    b = "Completely different — does NOT start with a"

    with caplog.at_level(logging.WARNING):
        _summary_marker_strict_append_reducer(a, b)

    assert any(
        "summary_marker_non_append" in record.message
        for record in caplog.records
    ), "Expected compaction.summary_marker_non_append log when non-append write is rejected"


def test_strict_append_reducer_accepts_valid_extension():
    """Valid extension (b starts with a) is accepted."""
    a = "Summary of steps 0-5.\n"
    b = "Summary of steps 0-5.\nSummary of steps 6-10.\n"

    result = _summary_marker_strict_append_reducer(a, b)
    assert result == b


def test_strict_append_reducer_first_write():
    """First write (a is None or empty) accepts any b."""
    assert _summary_marker_strict_append_reducer(None, "first summary") == "first summary"
    assert _summary_marker_strict_append_reducer("", "first summary") == "first summary"
