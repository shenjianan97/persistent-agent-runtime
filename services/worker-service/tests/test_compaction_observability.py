"""Observability structured-log events test (Track 7 AC 14 — automated portion).

AC 14 has two parts:
  - Automated: structured-log events fire at expected points (this file).
  - Manual: orchestrator verifies Langfuse UI via Playwright Scenario 16.

This file asserts that the following events fire and have correct shape when
the corresponding tiers are triggered:

  - ``compaction.tier1_applied`` — via Tier1AppliedEvent emitted by pipeline
  - ``compaction.tier15_applied`` — via Tier15AppliedEvent
  - ``compaction.tier3_fired`` — via Tier3FiredEvent
  - ``compaction.memory_flush_fired`` — via MemoryFlushFiredEvent

The pipeline returns events in a ``CompactionPassResult.events`` list;
the caller (agent_node) is responsible for logging them. This test asserts
the events have the right type and are emitted at the expected points.

Design doc: docs/design-docs/phase-2/track-7-context-window-management.md
§Observability.
"""

from __future__ import annotations

from typing import Any, Callable
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)

from executor.compaction.pipeline import (
    HardFloorEvent,
    MemoryFlushFiredEvent,
    Tier15AppliedEvent,
    Tier1AppliedEvent,
    Tier3FiredEvent,
    Tier3SkippedEvent,
    compact_for_llm,
)
from executor.compaction.summarizer import SummarizeResult
from executor.compaction.thresholds import resolve_thresholds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_pair(
    i: int,
    tool_name: str = "web_search",
    content: str = "result",
    arg_content: str = "x" * 10,
) -> list[BaseMessage]:
    call_id = f"call_{i}"
    return [
        AIMessage(
            content=f"Step {i}",
            tool_calls=[{
                "id": call_id,
                "name": tool_name,
                "args": {"content": arg_content},
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content=content,
            tool_call_id=call_id,
            name=tool_name,
        ),
    ]


def _make_messages(n: int, content_size: int = 200) -> list[BaseMessage]:
    msgs: list[BaseMessage] = [HumanMessage(content="task input")]
    for i in range(n):
        msgs.extend(_tool_pair(i, content="r" * content_size))
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


def _agent_config(
    exclude_tools: list[str] | None = None,
    memory_enabled: bool = False,
    pre_tier3_flush: bool = False,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "provider": "other",
        "model": "test-model",
        "context_management": {
            "exclude_tools": exclude_tools or [],
            "pre_tier3_memory_flush": pre_tier3_flush,
        },
        "memory": {
            "enabled": memory_enabled,
        },
    }
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


def _make_successful_summarizer(summary_text: str = "SUMMARY") -> AsyncMock:
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
# NOTE: ``compaction.per_result_capped`` / ``cap_tool_result`` were removed
# in Track 7 Follow-up Task 4. The replacement is ingestion offload — see
# ``test_compaction_ingestion_offload.py`` and ``test_tool_result_store.py``.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test: Tier1AppliedEvent fires at correct points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_applied_event_emitted_when_threshold_crossed():
    """Tier1AppliedEvent must be emitted when Tier 1 advances the watermark."""
    msgs = _make_messages(10, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)

    call_count = [0]
    def estimator(messages: list[BaseMessage]) -> int:
        call_count[0] += 1
        if call_count[0] == 1:
            return thresholds.tier1 + 500
        return thresholds.tier3 - 100

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=estimator,
    )

    tier1_events = [e for e in result.events if isinstance(e, Tier1AppliedEvent)]
    assert tier1_events, "Tier1AppliedEvent must be emitted when Tier 1 fires"

    ev = tier1_events[0]
    assert ev.messages_cleared > 0, "Tier1AppliedEvent.messages_cleared must be > 0"
    assert ev.new_watermark > 0, "Tier1AppliedEvent.new_watermark must be > 0"
    assert ev.task_id == "task-1"
    assert ev.tenant_id == "tenant-1"
    assert ev.agent_id == "agent-1"


# ---------------------------------------------------------------------------
# NOTE: Tier 1.5 (``truncate_tool_call_args``) was removed in Track 7 Follow-up
# Task 4. Oversized tool-call args are now offloaded to S3 at AIMessage-append
# time (see ``test_compaction_ingestion_offload.py``). The Tier15AppliedEvent
# class still exists for Task 3 to clean up, but the pipeline no longer fires
# the event.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test: Tier3FiredEvent fires at correct points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_fired_event_emitted_on_success():
    """Tier3FiredEvent must be emitted when Tier 3 succeeds."""
    msgs = _make_messages(5, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    SUMMARY = "The agent searched for info and found relevant data."

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(SUMMARY),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    tier3_events = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_events, "Tier3FiredEvent must be emitted on successful Tier 3"

    ev = tier3_events[0]
    assert ev.summarizer_model_id == "test-model"
    assert ev.tokens_in > 0 or ev.tokens_in == 100  # from mock
    assert ev.new_summarized_through > 0
    assert ev.task_id == "task-1"
    assert ev.tenant_id == "tenant-1"
    assert ev.agent_id == "agent-1"


# ---------------------------------------------------------------------------
# Test: MemoryFlushFiredEvent fires when memory enabled and conditions met
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_flush_fired_event_emitted_when_flush_fires():
    """MemoryFlushFiredEvent must be emitted when the pre-Tier-3 flush fires."""
    msgs = _make_messages(5, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,  # new work landed
    )

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(memory_enabled=True, pre_tier3_flush=True),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert flush_events, "MemoryFlushFiredEvent must be emitted when flush fires"

    ev = flush_events[0]
    assert ev.fired_at_step == len(msgs)
    assert ev.task_id == "task-1"
    assert ev.tenant_id == "tenant-1"
    assert ev.agent_id == "agent-1"


# ---------------------------------------------------------------------------
# Test: No events emitted on no-op path (below all thresholds)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tier_events_emitted_below_thresholds():
    """Below all thresholds, no tier events (only last_super_step_message_count update)."""
    msgs = _make_messages(2, content_size=5)
    model_context_window = 200_000

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(50),
    )

    tier_events = [
        e for e in result.events
        if isinstance(e, (
            Tier1AppliedEvent, Tier15AppliedEvent, Tier3FiredEvent,
            Tier3SkippedEvent, MemoryFlushFiredEvent, HardFloorEvent
        ))
    ]
    assert not tier_events, (
        f"No tier events must fire below all thresholds, got: {tier_events}"
    )


# ---------------------------------------------------------------------------
# Test: HardFloorEvent emitted when still over limit after all tiers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_floor_event_emitted_when_still_over_context():
    """HardFloorEvent must be emitted when all tiers cannot bring input below window."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(task_id="task-hardfloor"),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(model_context_window + 5000),
    )

    hard_floor_events = [e for e in result.events if isinstance(e, HardFloorEvent)]
    assert hard_floor_events, "HardFloorEvent must be emitted when still over context limit"

    ev = hard_floor_events[0]
    assert ev.est_tokens > model_context_window
    assert ev.model_context_window == model_context_window
    assert ev.task_id == "task-hardfloor"
