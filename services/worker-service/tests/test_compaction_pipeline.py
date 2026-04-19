"""Unit tests for compact_for_llm (Track 7 compaction pipeline).

Tests follow TDD: written before implementation.

Covers:
1. No-op: small history stays below Tier 1 threshold — no events
2. Tier 1 fires when input exceeds tier1 threshold
3. Tier 1.5 fires when input still > tier1 after Tier 1
4. Tier 3 fires only when Tier 1+1.5 cannot bring input below tier3
5. Tier 3 not re-fired when tier3_fatal_short_circuited = True
6. Tier 3 not fired when tier3_firings_count >= TIER_3_MAX_FIRINGS_PER_TASK
7. HardFloorEvent emitted when still over context after all tiers
8. summary_marker prepended as SystemMessage
9. state_updates contains last_super_step_message_count = len(raw_messages)
10. Cache-stability: two calls on same state → byte-identical output
11. Tier 3 skipped with 'retryable': watermark NOT advanced
12. Tier 3 skipped with 'fatal': tier3_fatal_short_circuited set True
13. Tier 3 success: watermark advanced, summary_marker updated
14. Tier 3 after first firing: appends to summary_marker
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from executor.compaction.defaults import (
    ARG_TRUNCATION_CAP_BYTES,
    KEEP_TOOL_USES,
    PLATFORM_EXCLUDE_TOOLS,
    TIER_3_MAX_FIRINGS_PER_TASK,
    TRUNCATABLE_TOOL_ARG_KEYS,
)
from executor.compaction.pipeline import (
    CompactionPassResult,
    HardFloorEvent,
    Tier1AppliedEvent,
    Tier15AppliedEvent,
    Tier3FiredEvent,
    Tier3SkippedEvent,
    compact_for_llm,
)
from executor.compaction.summarizer import SummarizeResult
from executor.compaction.thresholds import resolve_thresholds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_pair(
    i: int, content_size: int = 20, arg_size: int = 10
) -> list[BaseMessage]:
    """Build one (AIMessage + ToolMessage) pair."""
    call_id = f"call_{i}"
    return [
        AIMessage(
            content=f"Step {i}",
            tool_calls=[{
                "id": call_id,
                "name": f"tool_{i}",
                "args": {"content": "x" * arg_size},
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content="r" * content_size,
            tool_call_id=call_id,
            name=f"tool_{i}",
        ),
    ]


def _make_messages(n: int, content_size: int = 20, arg_size: int = 10) -> list[BaseMessage]:
    """Build n tool call pairs."""
    msgs: list[BaseMessage] = [HumanMessage(content="task input")]
    for i in range(n):
        msgs.extend(_make_tool_pair(i, content_size=content_size, arg_size=arg_size))
    return msgs


def _base_state(**overrides) -> dict[str, Any]:
    """Build a minimal RuntimeState dict with Track 7 defaults."""
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
    """Minimal agent config."""
    cfg: dict[str, Any] = {
        "provider": "other",
        "model": "test-model",
        "context_management": {},
    }
    cfg.update(overrides)
    return cfg


def _task_context(**overrides) -> dict[str, Any]:
    """Minimal task context."""
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
    """Returns an estimate_tokens callable that always returns n."""
    def estimator(messages: list[BaseMessage]) -> int:
        return n
    return estimator


def _token_counter_from_bytes(messages: list[BaseMessage]) -> int:
    """Heuristic token estimator using bytes/3."""
    total = sum(
        len(m.content.encode("utf-8")) if isinstance(m.content, str) else 0
        for m in messages
    )
    return total // 3 + len(messages)


def _make_successful_summarizer(summary_text: str = "SUMMARY") -> AsyncMock:
    """Mock summarizer that returns a successful SummarizeResult."""
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


def _make_retryable_summarizer() -> AsyncMock:
    """Mock summarizer that returns a retryable skip."""
    mock = AsyncMock()
    mock.return_value = SummarizeResult(
        summary_text=None,
        skipped=True,
        skipped_reason="retryable",
        summarizer_model_id="test-model",
        tokens_in=0,
        tokens_out=0,
        cost_microdollars=0,
        latency_ms=0,
    )
    return mock


def _make_fatal_summarizer() -> AsyncMock:
    """Mock summarizer that returns a fatal skip."""
    mock = AsyncMock()
    mock.return_value = SummarizeResult(
        summary_text=None,
        skipped=True,
        skipped_reason="fatal",
        summarizer_model_id="test-model",
        tokens_in=0,
        tokens_out=0,
        cost_microdollars=0,
        latency_ms=0,
    )
    return mock


# ---------------------------------------------------------------------------
# 1. No-op: small history below Tier 1 threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_small_history_no_events():
    """Small message history: nothing should fire."""
    msgs = _make_messages(2, content_size=5)
    model_context_window = 100_000  # large window
    thresholds = resolve_thresholds(model_context_window)

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(100),  # well below tier1
    )

    assert isinstance(result, CompactionPassResult)
    # No tier events
    tier_events = [e for e in result.events if isinstance(e, (Tier1AppliedEvent, Tier15AppliedEvent, Tier3FiredEvent, Tier3SkippedEvent, HardFloorEvent))]
    assert not tier_events, f"Unexpected events: {tier_events}"
    # Messages unchanged
    assert result.messages is msgs or result.messages == msgs
    # last_super_step_message_count must be updated
    assert result.state_updates.get("last_super_step_message_count") == len(msgs)


# ---------------------------------------------------------------------------
# 2. Tier 1 fires when input exceeds tier1 threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_fires_when_over_threshold():
    """When estimated tokens > tier1, clear_tool_results must fire."""
    # Use enough tool pairs so there are clearable messages
    msgs = _make_messages(10, content_size=500)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)

    # Set token estimate just above tier1
    token_count = thresholds.tier1 + 500

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    tier1_events = [e for e in result.events if isinstance(e, Tier1AppliedEvent)]
    assert tier1_events, "Tier1AppliedEvent must be emitted when threshold exceeded"
    assert result.state_updates.get("cleared_through_turn_index", 0) > 0


# ---------------------------------------------------------------------------
# 3. Tier 1.5 fires after Tier 1 when still above threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier15_fires_after_tier1():
    """When Tier 1 alone is insufficient, Tier 1.5 (arg truncation) should fire."""
    # Create messages with large args that are truncatable
    msgs = [HumanMessage(content="task")]
    for i in range(10):
        call_id = f"call_{i}"
        msgs.extend([
            AIMessage(
                content=f"Step {i}",
                tool_calls=[{
                    "id": call_id,
                    "name": f"tool_{i}",
                    "args": {"content": "x" * 5000},  # large truncatable arg
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content="r" * 100,
                tool_call_id=call_id,
                name=f"tool_{i}",
            ),
        ])

    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier1 + 100

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    tier15_events = [e for e in result.events if isinstance(e, Tier15AppliedEvent)]
    assert tier15_events, "Tier15AppliedEvent must be emitted"
    assert result.state_updates.get("truncated_args_through_turn_index", 0) > 0


# ---------------------------------------------------------------------------
# 4. Tier 3 fires only when Tier 1+1.5 cannot bring input below tier3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_fires_only_when_tier1_insufficient():
    """Tier 3 must fire only when input exceeds tier3 threshold after Tier 1+1.5."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)

    # Estimate stays above tier3 even after Tier 1+1.5
    token_count = thresholds.tier3 + 1000
    summarizer = _make_successful_summarizer("Summary of old steps.")

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    tier3_events = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_events, "Tier3FiredEvent must fire when input > tier3 threshold"
    assert result.state_updates.get("tier3_firings_count", 0) > 0


# ---------------------------------------------------------------------------
# 5. Tier 3 NOT re-fired when tier3_fatal_short_circuited = True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_skipped_when_fatal_short_circuited():
    """When tier3_fatal_short_circuited=True, summarizer must NOT be called."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(tier3_fatal_short_circuited=True),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    summarizer.assert_not_called()
    tier3_events = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert not tier3_events, "Tier 3 must NOT fire when short-circuited"


# ---------------------------------------------------------------------------
# 6. Tier 3 skipped when tier3_firings_count >= TIER_3_MAX_FIRINGS_PER_TASK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_skipped_when_cap_reached():
    """When firings count is at/over cap, Tier 3 is skipped with cap_reached."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(tier3_firings_count=TIER_3_MAX_FIRINGS_PER_TASK),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    summarizer.assert_not_called()
    skipped_events = [e for e in result.events if isinstance(e, Tier3SkippedEvent)]
    assert skipped_events, "Tier3SkippedEvent must be emitted when cap reached"
    assert skipped_events[0].reason == "cap_reached"


# ---------------------------------------------------------------------------
# 7. HardFloorEvent when still over context after all tiers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_floor_event_emitted_when_still_over_limit():
    """When all tiers fail to bring input below model window, HardFloorEvent is emitted."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000

    # Token estimate that is always over the context window regardless of compaction
    # Simulate: pipeline runs Tier 1, 1.5, Tier 3 but still over
    summarizer = _make_successful_summarizer("Summary")

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(model_context_window + 5000),
    )

    hard_floor_events = [e for e in result.events if isinstance(e, HardFloorEvent)]
    assert hard_floor_events, "HardFloorEvent must be emitted when still over limit"


# ---------------------------------------------------------------------------
# 8. summary_marker prepended as SystemMessage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_marker_prepended_as_system_message():
    """When summary_marker is non-empty, pipeline prepends it as SystemMessage."""
    msgs = [HumanMessage(content="task input")]
    model_context_window = 100_000
    existing_marker = "Summary of prior steps.\n"

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(summary_marker=existing_marker),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(50),
    )

    # First message must be a SystemMessage containing the summary
    assert result.messages, "Messages must not be empty"
    first = result.messages[0]
    assert isinstance(first, SystemMessage), "summary_marker must be prepended as SystemMessage"
    assert existing_marker in first.content
    # Must carry compaction kwarg for Langfuse visibility
    assert first.additional_kwargs.get("compaction") is True


# ---------------------------------------------------------------------------
# 9. state_updates always contains last_super_step_message_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_updates_includes_last_super_step_message_count():
    msgs = _make_messages(3, content_size=10)
    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=100_000,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(100),
    )
    assert "last_super_step_message_count" in result.state_updates
    assert result.state_updates["last_super_step_message_count"] == len(msgs)


# ---------------------------------------------------------------------------
# 10. Cache-stability: two calls on same state → byte-identical output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_stability_identical_output_on_second_call():
    """Running pipeline twice on the same state must produce byte-identical output."""
    msgs = _make_messages(8, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier1 + 100

    state = _base_state()
    config = _agent_config()
    ctx = _task_context()
    summarizer = _make_successful_summarizer("DETERMINISTIC_SUMMARY")

    result1 = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    # Second call with the same state (not updated state — same watermarks)
    result2 = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    # Messages must be byte-identical
    assert len(result1.messages) == len(result2.messages)
    for m1, m2 in zip(result1.messages, result2.messages):
        assert type(m1) == type(m2)
        assert m1.content == m2.content

    # State updates must be identical
    assert result1.state_updates == result2.state_updates


# ---------------------------------------------------------------------------
# 11. Tier 3 skipped with 'retryable': watermark NOT advanced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_retryable_skip_watermark_not_advanced():
    """When summarizer returns retryable skip, summarized_through_turn_index stays."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    initial_watermark = 0
    summarizer = _make_retryable_summarizer()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(summarized_through_turn_index=initial_watermark),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    assert result.state_updates.get(
        "summarized_through_turn_index", initial_watermark
    ) == initial_watermark, "Watermark must NOT advance on retryable skip"

    skipped = [e for e in result.events if isinstance(e, Tier3SkippedEvent)]
    assert skipped, "Tier3SkippedEvent must be emitted on retryable skip"


# ---------------------------------------------------------------------------
# 12. Tier 3 skipped with 'fatal': tier3_fatal_short_circuited set True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_fatal_sets_short_circuit_flag():
    """When summarizer returns fatal skip, tier3_fatal_short_circuited must be True."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_fatal_summarizer()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    assert result.state_updates.get("tier3_fatal_short_circuited") is True

    # Second call: summarizer must NOT be called again
    summarizer2 = _make_successful_summarizer()
    result2 = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(tier3_fatal_short_circuited=True),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer2,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )
    summarizer2.assert_not_called()


# ---------------------------------------------------------------------------
# 13. Tier 3 success: watermark advanced and summary_marker updated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_success_advances_watermark_and_updates_marker():
    """Successful Tier 3: summarized_through_turn_index advances, summary_marker updated."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summary_text = "Summary of steps 0 through 4."
    summarizer = _make_successful_summarizer(summary_text)

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    tier3_events = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_events

    new_watermark = result.state_updates.get("summarized_through_turn_index", 0)
    assert new_watermark > 0, "Watermark must advance on Tier 3 success"

    new_marker = result.state_updates.get("summary_marker", "")
    assert summary_text in new_marker, "summary_marker must contain the summary text"

    # tier3_firings_count must increment
    assert result.state_updates.get("tier3_firings_count", 0) == 1


# ---------------------------------------------------------------------------
# 14. Second Tier 3 firing appends to summary_marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_tier3_appends_to_summary_marker():
    """When Tier 3 fires again, the new summary must be appended to the existing marker."""
    msgs = _make_messages(10, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    first_summary = "Summary of steps 0-4."
    second_summary = "Summary of steps 0-4.\nSummary of steps 5-9."

    # First call: no prior marker
    summarizer_first = _make_successful_summarizer(first_summary)
    result1 = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer_first,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )
    new_marker = result1.state_updates.get("summary_marker", "")
    assert first_summary in new_marker

    # Second call: with existing marker, summarizer returns extended string
    summarizer_second = _make_successful_summarizer(second_summary)
    result2 = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(
            summary_marker=new_marker,
            tier3_firings_count=result1.state_updates.get("tier3_firings_count", 1),
            summarized_through_turn_index=result1.state_updates.get("summarized_through_turn_index", 0),
        ),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer_second,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    final_marker = result2.state_updates.get("summary_marker", "")
    assert second_summary in final_marker or first_summary in final_marker


# ---------------------------------------------------------------------------
# 15. Tier 3 not fired when Tier 1+1.5 brought below tier3 threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_not_fired_when_tier1_sufficient():
    """If Tier 1+1.5 brings estimated tokens below tier3 threshold, Tier 3 must be skipped."""
    # We set up a token counter that returns different values based on message count
    # After Tier 1 clears old results, estimate_tokens_fn is called again — we simulate
    # this by returning tier3-1 on the second call
    msgs = _make_messages(10, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)

    # First call: above tier1, second call: below tier3
    call_count = [0]
    def decreasing_estimator(messages: list) -> int:
        call_count[0] += 1
        if call_count[0] == 1:
            return thresholds.tier1 + 500  # Trigger Tier 1
        return thresholds.tier3 - 100  # After Tier 1, below tier3

    summarizer = _make_successful_summarizer()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=decreasing_estimator,
    )

    tier3_events = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert not tier3_events, "Tier 3 must NOT fire when Tier 1+1.5 resolved the issue"
    summarizer.assert_not_called()
