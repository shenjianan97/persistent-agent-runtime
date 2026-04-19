"""Memory-disabled agents never fire the pre-Tier-3 flush (Track 7 AC 13).

AC 13: When ``memory.enabled = false``, the pre-Tier-3 memory flush MUST NOT
fire even if ``context_management.pre_tier3_memory_flush = true`` in the agent
config.

Design doc: docs/design-docs/phase-2/track-7-context-window-management.md
§Pre-Tier-3 memory flush — "Opt-out paths" and §Validation rule #8.
"""

from __future__ import annotations

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
    MemoryFlushFiredEvent,
    Tier3FiredEvent,
    compact_for_llm,
)
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
# Test: memory disabled — flush never fires even when pre_tier3_memory_flush=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_disabled_flush_never_fires():
    """When memory.enabled=false, the pre-Tier-3 flush MUST NOT fire.

    The flush setting pre_tier3_memory_flush=true is irrelevant when memory
    is disabled — there is no memory system to flush into.
    """
    msgs = _make_messages(5, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    agent_config = {
        "provider": "other",
        "model": "test-model",
        "context_management": {
            "pre_tier3_memory_flush": True,  # explicitly enabled
        },
        "memory": {
            "enabled": False,  # memory disabled — flush must not fire
        },
    }

    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,  # new work landed
    )

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=agent_config,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert not flush_events, (
        "MemoryFlushFiredEvent must NOT be emitted when memory.enabled=false "
        "(AC 13: memory-disabled agents never fire the flush)"
    )

    # Tier 3 must still fire normally
    tier3_events = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_events, (
        "Tier 3 must fire normally even when memory is disabled"
    )


@pytest.mark.asyncio
async def test_memory_disabled_no_flush_system_message_in_compacted():
    """When memory.enabled=false, the compacted messages must NOT include the flush prompt.

    No SystemMessage with compaction_event='pre_tier3_memory_flush' should appear
    in the result messages.
    """
    msgs = _make_messages(5, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    agent_config = {
        "provider": "other",
        "model": "test-model",
        "context_management": {
            "pre_tier3_memory_flush": True,
        },
        "memory": {
            "enabled": False,
        },
    }

    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=agent_config,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    # No flush SystemMessage must appear in the compacted messages
    flush_msgs = [
        m for m in result.messages
        if isinstance(m, SystemMessage)
        and m.additional_kwargs.get("compaction_event") == "pre_tier3_memory_flush"
    ]
    assert not flush_msgs, (
        "No flush SystemMessage must appear in compacted messages when memory is disabled "
        "(AC 13: memory-disabled agents never fire pre-Tier-3 flush)"
    )


@pytest.mark.asyncio
async def test_memory_disabled_memory_flush_flag_not_set():
    """When memory.enabled=false, memory_flush_fired_this_task must NOT be set to True."""
    msgs = _make_messages(5, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    agent_config = {
        "provider": "other",
        "model": "test-model",
        "context_management": {
            "pre_tier3_memory_flush": True,
        },
        "memory": {
            "enabled": False,
        },
    }

    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=agent_config,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    # memory_flush_fired_this_task must NOT have been set to True
    flag = result.state_updates.get("memory_flush_fired_this_task", False)
    assert flag is not True, (
        "memory_flush_fired_this_task must NOT be set to True when memory is disabled "
        "(AC 13)"
    )


@pytest.mark.asyncio
async def test_memory_absent_from_config_flush_never_fires():
    """When the 'memory' key is absent from agent_config, the flush must not fire.

    Agents without a memory configuration have memory implicitly disabled.
    """
    msgs = _make_messages(5, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    agent_config = {
        "provider": "other",
        "model": "test-model",
        "context_management": {
            "pre_tier3_memory_flush": True,
        },
        # No 'memory' key at all — treated as memory.enabled=false
    }

    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=agent_config,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert not flush_events, (
        "MemoryFlushFiredEvent must NOT fire when memory key is absent from config"
    )

    # Tier 3 must still proceed normally
    tier3_events = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_events, "Tier 3 must fire normally when memory is absent"


@pytest.mark.asyncio
async def test_memory_enabled_true_flush_fires_when_conditions_met():
    """Positive control: flush DOES fire when memory.enabled=true.

    This ensures we're testing the correct condition and not a vacuous always-false.
    """
    msgs = _make_messages(5, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    agent_config = {
        "provider": "other",
        "model": "test-model",
        "context_management": {
            "pre_tier3_memory_flush": True,
        },
        "memory": {
            "enabled": True,  # memory enabled
        },
    }

    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,  # new work landed
    )

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=agent_config,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    # When memory IS enabled, flush should fire (positive control for AC 13)
    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert flush_events, (
        "MemoryFlushFiredEvent must fire when memory.enabled=true and conditions are met "
        "(positive control to validate the memory-disabled test)"
    )
