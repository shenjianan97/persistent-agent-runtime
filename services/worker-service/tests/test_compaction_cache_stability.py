"""Cache-stability invariant test (Track 7 AC 5).

AC 5: Running the same compaction pipeline on the same state twice produces
byte-identical output (same messages content, same state_updates dict).

This property ensures that KV-cache prefixes remain stable across repeated
LLM calls within a task — identical prefix bytes are the prerequisite for
a cache hit on the provider side.

See docs/design-docs/phase-2/track-7-context-window-management.md §Validation
rule #2 and §Cache-stability invariant.
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
    CompactionPassResult,
    compact_for_llm,
)
from executor.compaction.summarizer import SummarizeResult
from executor.compaction.thresholds import resolve_thresholds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_pair(i: int, content_size: int = 200, arg_size: int = 50) -> list[BaseMessage]:
    call_id = f"call_{i}"
    return [
        AIMessage(
            content=f"Step {i}: doing work",
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


def _make_messages(n: int, content_size: int = 200) -> list[BaseMessage]:
    msgs: list[BaseMessage] = [HumanMessage(content="This is the task input.")]
    for i in range(n):
        msgs.extend(_make_tool_pair(i, content_size=content_size))
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


def _make_deterministic_summarizer(summary_text: str = "STABLE_SUMMARY") -> AsyncMock:
    """Mock summarizer that always returns the same text."""
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


def _messages_content(messages: list[BaseMessage]) -> list[str]:
    """Extract content strings from a message list for deep equality checking."""
    parts = []
    for m in messages:
        if isinstance(m.content, str):
            parts.append(m.content)
        else:
            parts.append(str(m.content))
    return parts


# ---------------------------------------------------------------------------
# Test: Tier 1 path — cache stability when only masking fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_stability_tier1_only():
    """Running the pipeline twice with only Tier 1 firing yields byte-identical output."""
    msgs = _make_messages(10, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    # Above Tier 1 but below Tier 3
    token_count = thresholds.tier1 + 500

    state = _base_state()
    config = _agent_config()
    ctx = _task_context()
    summarizer = _make_deterministic_summarizer()

    result1 = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    result2 = await compact_for_llm(
        raw_messages=msgs,
        state=state,  # same state — watermarks NOT updated between calls
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    # Byte-identical message content
    assert _messages_content(result1.messages) == _messages_content(result2.messages), (
        "Cache-stability violated: Tier-1-only pass produced different message content"
    )

    # Types must also match
    assert [type(m).__name__ for m in result1.messages] == [
        type(m).__name__ for m in result2.messages
    ]

    # State updates must be identical
    assert result1.state_updates == result2.state_updates, (
        "Cache-stability violated: Tier-1-only pass produced different state_updates"
    )


# ---------------------------------------------------------------------------
# Test: Tier 1 + Tier 1.5 path — cache stability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_stability_tier1_and_tier15():
    """Running the pipeline twice with Tier 1 + 1.5 firing yields byte-identical output."""
    msgs: list[BaseMessage] = [HumanMessage(content="task")]
    for i in range(10):
        call_id = f"call_{i}"
        msgs.extend([
            AIMessage(
                content=f"Step {i}",
                tool_calls=[{
                    "id": call_id,
                    "name": f"tool_{i}",
                    "args": {"content": "x" * 3000},  # large truncatable arg
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

    state = _base_state()
    config = _agent_config()
    ctx = _task_context()
    summarizer = _make_deterministic_summarizer()

    result1 = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    result2 = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    assert _messages_content(result1.messages) == _messages_content(result2.messages), (
        "Cache-stability violated: Tier-1+1.5 pass produced different message content"
    )
    assert result1.state_updates == result2.state_updates, (
        "Cache-stability violated: Tier-1+1.5 pass produced different state_updates"
    )


# ---------------------------------------------------------------------------
# Test: Tier 3 path — cache stability (deterministic summarizer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_stability_tier3_fires():
    """Running the pipeline twice with Tier 3 firing produces byte-identical output.

    Both calls must produce the same state_updates dict including summary_marker.
    """
    msgs = _make_messages(8, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    # Always above Tier 3
    token_count = thresholds.tier3 + 1000

    FIXED_SUMMARY = "Deterministic summary text for cache test."
    state = _base_state()
    config = _agent_config()
    ctx = _task_context()
    # Use two separate mocks that both return the same text
    summarizer_a = _make_deterministic_summarizer(FIXED_SUMMARY)
    summarizer_b = _make_deterministic_summarizer(FIXED_SUMMARY)

    result1 = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer_a,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    result2 = await compact_for_llm(
        raw_messages=msgs,
        state=state,  # same state — not the updated state
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer_b,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    # Byte-identical message content on both calls
    assert _messages_content(result1.messages) == _messages_content(result2.messages), (
        "Cache-stability violated: Tier-3 pass produced different message content"
    )

    # State updates must be identical
    assert result1.state_updates == result2.state_updates, (
        "Cache-stability violated: Tier-3 pass produced different state_updates"
    )


# ---------------------------------------------------------------------------
# Test: pre-existing summary_marker — cache stability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_stability_with_existing_summary_marker():
    """Cache stability holds when a summary_marker is already in state.

    The marker must appear as the first SystemMessage on both calls with
    identical content.
    """
    existing_marker = "Summary of steps 0-4.\n"
    msgs = _make_messages(4, content_size=20)
    model_context_window = 100_000

    state = _base_state(
        summary_marker=existing_marker,
        summarized_through_turn_index=4,
    )
    config = _agent_config()
    ctx = _task_context()
    summarizer = _make_deterministic_summarizer()

    result1 = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(50),  # well below any threshold
    )

    result2 = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(50),
    )

    # Both results must start with the same summary marker SystemMessage
    assert result1.messages, "Messages must not be empty"
    assert result2.messages, "Messages must not be empty"

    first1 = result1.messages[0]
    first2 = result2.messages[0]
    assert isinstance(first1, SystemMessage)
    assert isinstance(first2, SystemMessage)
    assert first1.content == first2.content, (
        "Summary marker SystemMessage content must be byte-identical on second call"
    )

    assert _messages_content(result1.messages) == _messages_content(result2.messages)
    assert result1.state_updates == result2.state_updates


# ---------------------------------------------------------------------------
# Test: No-op path is cache-stable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_stability_no_op_path():
    """No-op path (below all thresholds) produces identical output on repeated calls."""
    msgs = _make_messages(2, content_size=5)
    model_context_window = 200_000
    state = _base_state()
    config = _agent_config()
    ctx = _task_context()
    summarizer = _make_deterministic_summarizer()

    result1 = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(50),
    )

    result2 = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=config,
        model_context_window=model_context_window,
        task_context=ctx,
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(50),
    )

    assert _messages_content(result1.messages) == _messages_content(result2.messages)
    assert result1.state_updates == result2.state_updates
