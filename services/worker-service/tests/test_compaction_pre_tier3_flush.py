"""Unit tests for pre-Tier-3 memory flush (Task 9).

Tests follow TDD: written before implementation.

Covers:
1. Fires when all 4 conditions true AND over Tier 3 threshold
2. Does NOT fire when pre_tier3_memory_flush=False
3. Does NOT fire when memory.enabled=False (even if pre_tier3_memory_flush=True)
4. Does NOT fire when memory_flush_fired_this_task=True (one-shot)
5. Does NOT fire on heartbeat turn (len(raw_messages) <= last_super_step_message_count)
6. When fired: Tier 3 is SKIPPED this call; memory_flush_fired_this_task advances to True
7. last_super_step_message_count updated every call (regardless of flush outcome)
8. _is_heartbeat_turn positional detection (rate-limit retry fixture)
9. _is_heartbeat_turn positional detection (normal new work fixture)
10. Flush SystemMessage is appended at END of compacted messages
11. Flush SystemMessage content matches _PRE_TIER3_FLUSH_PROMPT exactly
12. Flush message NOT persisted to graph state (not in state_updates["messages"])
13. Redrive safety: flush does NOT re-fire when memory_flush_fired_this_task=True restored
14. MemoryFlushFiredEvent emitted when flush fires
15. MemoryFlushFiredEvent NOT emitted when heartbeat skips flush
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
    MemoryFlushFiredEvent,
    Tier3FiredEvent,
    Tier3SkippedEvent,
    _PRE_TIER3_FLUSH_PROMPT,
    _is_heartbeat_turn,
    compact_for_llm,
    should_fire_pre_tier3_flush,
)
from executor.compaction.summarizer import SummarizeResult
from executor.compaction.thresholds import resolve_thresholds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_pair(i: int, content_size: int = 20) -> list[BaseMessage]:
    """Build one (AIMessage + ToolMessage) pair."""
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


def _make_messages(n: int, content_size: int = 20) -> list[BaseMessage]:
    """Build n tool call pairs."""
    msgs: list[BaseMessage] = [HumanMessage(content="task input")]
    for i in range(n):
        msgs.extend(_make_tool_pair(i, content_size=content_size))
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


def _agent_config_with_memory(**overrides) -> dict[str, Any]:
    """Agent config with memory.enabled=True and pre_tier3_memory_flush=True (defaults)."""
    cfg: dict[str, Any] = {
        "provider": "other",
        "model": "test-model",
        "context_management": {
            "pre_tier3_memory_flush": True,
        },
        "memory": {
            "enabled": True,
        },
    }
    cfg.update(overrides)
    return cfg


def _agent_config_no_memory(**overrides) -> dict[str, Any]:
    """Agent config with memory.enabled=False."""
    cfg: dict[str, Any] = {
        "provider": "other",
        "model": "test-model",
        "context_management": {
            "pre_tier3_memory_flush": True,
        },
        "memory": {
            "enabled": False,
        },
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


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


def test_is_heartbeat_turn_returns_true_when_no_new_messages():
    """_is_heartbeat_turn returns True when message count has not grown."""
    raw_messages = _make_messages(3)  # 7 messages (1 human + 3 pairs)
    last_count = len(raw_messages)  # same as current
    assert _is_heartbeat_turn(raw_messages, last_count) is True


def test_is_heartbeat_turn_returns_false_when_new_messages():
    """_is_heartbeat_turn returns False when new messages have been appended."""
    raw_messages = _make_messages(3)  # 7 messages
    last_count = len(raw_messages) - 1  # one less than current — new work landed
    assert _is_heartbeat_turn(raw_messages, last_count) is False


def test_is_heartbeat_turn_returns_true_when_count_equal():
    """_is_heartbeat_turn returns True when count is exactly equal (boundary)."""
    raw_messages = _make_messages(2)
    last_count = len(raw_messages)
    assert _is_heartbeat_turn(raw_messages, last_count) is True


def test_is_heartbeat_turn_returns_true_when_count_higher():
    """_is_heartbeat_turn returns True when last_count > current (stale watermark)."""
    raw_messages = _make_messages(2)
    last_count = len(raw_messages) + 5  # somehow higher
    assert _is_heartbeat_turn(raw_messages, last_count) is True


def test_is_heartbeat_turn_rate_limit_retry_fixture():
    """Rate-limit retry: consecutive AIMessages but message count unchanged → heartbeat."""
    # Simulate: prior call wrote an AIMessage (id=1), retry context has same count
    ai_msg_1 = AIMessage(content="thinking...", id="msg-1")
    ai_msg_2 = AIMessage(content="retrying...", id="msg-2")  # rate-limit retry
    raw_messages = [HumanMessage(content="task"), ai_msg_1, ai_msg_2]
    # last_super_step_message_count was set to 3 at end of previous call
    last_count = len(raw_messages)  # == 3, no new tool results
    assert _is_heartbeat_turn(raw_messages, last_count) is True


def test_is_heartbeat_turn_normal_tool_call_fixture():
    """After tool result lands, message count advances → not a heartbeat."""
    # Simulate: last super-step saw 3 messages, now a tool result was appended
    call_id = "call_0"
    raw_messages = [
        HumanMessage(content="task"),
        AIMessage(content="step", tool_calls=[{
            "id": call_id, "name": "tool_0", "args": {}, "type": "tool_call"
        }]),
        ToolMessage(content="result", tool_call_id=call_id, name="tool_0"),
    ]
    # last count before the ToolMessage landed
    last_count = 2
    assert _is_heartbeat_turn(raw_messages, last_count) is False


def test_should_fire_pre_tier3_flush_all_conditions_true():
    """should_fire_pre_tier3_flush returns True when all 4 conditions are satisfied."""
    msgs = _make_messages(5)
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,  # new message landed
    )
    cfg = _agent_config_with_memory()
    assert should_fire_pre_tier3_flush(state, cfg, msgs) is True


def test_should_fire_pre_tier3_flush_false_when_disabled_in_config():
    """should_fire_pre_tier3_flush returns False when pre_tier3_memory_flush=False."""
    msgs = _make_messages(5)
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()
    cfg["context_management"]["pre_tier3_memory_flush"] = False
    assert should_fire_pre_tier3_flush(state, cfg, msgs) is False


def test_should_fire_pre_tier3_flush_false_when_memory_disabled():
    """should_fire_pre_tier3_flush returns False when memory.enabled=False."""
    msgs = _make_messages(5)
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_no_memory()
    assert should_fire_pre_tier3_flush(state, cfg, msgs) is False


def test_should_fire_pre_tier3_flush_false_when_already_fired():
    """should_fire_pre_tier3_flush returns False when memory_flush_fired_this_task=True."""
    msgs = _make_messages(5)
    state = _base_state(
        memory_flush_fired_this_task=True,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()
    assert should_fire_pre_tier3_flush(state, cfg, msgs) is False


def test_should_fire_pre_tier3_flush_false_on_heartbeat():
    """should_fire_pre_tier3_flush returns False when it's a heartbeat turn."""
    msgs = _make_messages(5)
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs),  # equal → heartbeat
    )
    cfg = _agent_config_with_memory()
    assert should_fire_pre_tier3_flush(state, cfg, msgs) is False


def test_should_fire_pre_tier3_flush_default_true_for_missing_config_key():
    """pre_tier3_memory_flush defaults to True when key is absent from config."""
    msgs = _make_messages(5)
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    # context_management present but pre_tier3_memory_flush key absent
    cfg = {
        "provider": "other",
        "model": "test-model",
        "context_management": {},
        "memory": {"enabled": True},
    }
    assert should_fire_pre_tier3_flush(state, cfg, msgs) is True


# ---------------------------------------------------------------------------
# Pipeline-level tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_fires_when_all_conditions_true_and_over_tier3():
    """Flush fires when all 4 conditions true AND over Tier 3 threshold."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,  # new work landed
    )
    cfg = _agent_config_with_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    # Flush event must be emitted
    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert flush_events, "MemoryFlushFiredEvent must be emitted when flush fires"

    # memory_flush_fired_this_task must be True in state_updates
    assert result.state_updates.get("memory_flush_fired_this_task") is True

    # Tier 3 must NOT have fired this call
    tier3_fired = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert not tier3_fired, "Tier 3 must be skipped when flush fires"

    # Summarizer must NOT have been called
    summarizer.assert_not_called()


@pytest.mark.asyncio
async def test_flush_does_not_fire_when_config_disabled():
    """Flush does NOT fire when pre_tier3_memory_flush=False."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()
    cfg["context_management"]["pre_tier3_memory_flush"] = False

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert not flush_events, "Flush must NOT fire when pre_tier3_memory_flush=False"

    # Tier 3 should proceed normally
    tier3_fired = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_fired, "Tier 3 must proceed when flush is disabled"


@pytest.mark.asyncio
async def test_flush_does_not_fire_when_memory_disabled():
    """Flush does NOT fire when memory.enabled=False, even with pre_tier3_memory_flush=True."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_no_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert not flush_events, "Flush must NOT fire when memory.enabled=False"

    # Tier 3 should proceed normally
    tier3_fired = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_fired, "Tier 3 must proceed normally when memory is disabled"


@pytest.mark.asyncio
async def test_flush_does_not_fire_when_already_fired():
    """Flush does NOT re-fire when memory_flush_fired_this_task=True (one-shot)."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=True,  # already fired
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert not flush_events, "Flush must NOT re-fire (one-shot)"

    # Tier 3 must proceed on the second call
    tier3_fired = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_fired, "Tier 3 must proceed after flush has already fired"


@pytest.mark.asyncio
async def test_flush_does_not_fire_on_heartbeat_turn():
    """Flush does NOT fire on heartbeat turn (no new messages since last super-step)."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs),  # same count → heartbeat
    )
    cfg = _agent_config_with_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert not flush_events, "Flush must NOT fire on heartbeat turn"

    # Tier 3 must proceed
    tier3_fired = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_fired, "Tier 3 must proceed on heartbeat turn"


@pytest.mark.asyncio
async def test_flush_message_appended_at_end_of_compacted_messages():
    """When flush fires, SystemMessage is appended at END of compacted messages."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    # Flush events must have fired
    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert flush_events, "Expected flush to fire"

    # Last message must be the flush SystemMessage
    assert result.messages, "Messages must not be empty"
    last_msg = result.messages[-1]
    assert isinstance(last_msg, SystemMessage), "Last message must be a SystemMessage"
    assert last_msg.additional_kwargs.get("compaction_event") == "pre_tier3_memory_flush"


@pytest.mark.asyncio
async def test_flush_message_content_matches_prompt_exactly():
    """Flush SystemMessage content must exactly match _PRE_TIER3_FLUSH_PROMPT."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert flush_events, "Expected flush to fire"

    last_msg = result.messages[-1]
    assert isinstance(last_msg, SystemMessage)
    assert last_msg.content == _PRE_TIER3_FLUSH_PROMPT, (
        "Flush message content must byte-exactly match _PRE_TIER3_FLUSH_PROMPT"
    )


@pytest.mark.asyncio
async def test_flush_message_not_persisted_to_state_updates():
    """Flush SystemMessage must NOT appear in state_updates (only in-memory for LLM call)."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert flush_events, "Expected flush to fire"

    # state_updates must NOT contain a "messages" key with the flush message
    persisted_messages = result.state_updates.get("messages", [])
    flush_in_state = [
        m for m in persisted_messages
        if isinstance(m, SystemMessage)
        and m.additional_kwargs.get("compaction_event") == "pre_tier3_memory_flush"
    ]
    assert not flush_in_state, (
        "Flush SystemMessage must NOT be persisted to state_updates['messages']"
    )


@pytest.mark.asyncio
async def test_last_super_step_message_count_updated_on_flush():
    """last_super_step_message_count is updated every call when flush fires."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    assert result.state_updates.get("last_super_step_message_count") == len(msgs)


@pytest.mark.asyncio
async def test_last_super_step_message_count_updated_on_normal_call():
    """last_super_step_message_count is updated every call even when flush doesn't fire."""
    msgs = _make_messages(3, content_size=5)
    model_context_window = 100_000

    summarizer = _make_successful_summarizer()
    state = _base_state(last_super_step_message_count=0)
    cfg = _agent_config_no_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(100),
    )

    assert result.state_updates.get("last_super_step_message_count") == len(msgs)


@pytest.mark.asyncio
async def test_redrive_safety_flush_does_not_refire():
    """Redrive safety: flush does NOT re-fire when restored from checkpoint with flag=True."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    # Simulate checkpoint restore: memory_flush_fired_this_task=True preserved
    state = _base_state(
        memory_flush_fired_this_task=True,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert not flush_events, "Flush must NOT re-fire on redrive from post-flush checkpoint"

    # Tier 3 must proceed
    tier3_fired = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_fired, "Tier 3 must proceed after flush already fired"


@pytest.mark.asyncio
async def test_memory_flush_event_not_emitted_on_heartbeat_skip():
    """MemoryFlushFiredEvent is NOT emitted when heartbeat detection skips flush."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs),  # heartbeat
    )
    cfg = _agent_config_with_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert not flush_events, "MemoryFlushFiredEvent must NOT be emitted on heartbeat skip"


@pytest.mark.asyncio
async def test_flush_fires_only_once_across_two_calls():
    """Two sequential calls: flush fires on call 1, NOT on call 2 (flag sticks)."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer_1 = _make_successful_summarizer()
    state_1 = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()

    result_1 = await compact_for_llm(
        raw_messages=msgs,
        state=state_1,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer_1,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events_1 = [e for e in result_1.events if isinstance(e, MemoryFlushFiredEvent)]
    assert flush_events_1, "Flush must fire on call 1"
    assert result_1.state_updates.get("memory_flush_fired_this_task") is True

    # Call 2: simulate state updated with the flag from call 1
    state_2 = _base_state(
        memory_flush_fired_this_task=True,  # flag set by call 1
        last_super_step_message_count=len(msgs) - 1,
    )
    summarizer_2 = _make_successful_summarizer()

    result_2 = await compact_for_llm(
        raw_messages=msgs,
        state=state_2,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer_2,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events_2 = [e for e in result_2.events if isinstance(e, MemoryFlushFiredEvent)]
    assert not flush_events_2, "Flush must NOT fire on call 2 (one-shot)"

    # Tier 3 must now proceed
    tier3_fired_2 = [e for e in result_2.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_fired_2, "Tier 3 must proceed on call 2 after flush already fired"


@pytest.mark.asyncio
async def test_flush_does_not_fire_below_tier3_threshold():
    """Flush must NOT fire when token count is below Tier 3 threshold."""
    msgs = _make_messages(3, content_size=10)
    model_context_window = 100_000
    thresholds = resolve_thresholds(model_context_window)

    # Token estimate below Tier 3 threshold
    token_count = thresholds.tier3 - 100

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert not flush_events, "Flush must NOT fire when below Tier 3 threshold"


@pytest.mark.asyncio
async def test_flush_system_message_has_correct_additional_kwargs():
    """Flush SystemMessage has compaction=True and compaction_event=pre_tier3_memory_flush."""
    msgs = _make_messages(5, content_size=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    summarizer = _make_successful_summarizer()
    state = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    cfg = _agent_config_with_memory()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state,
        agent_config=cfg,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert flush_events, "Expected flush to fire"

    last_msg = result.messages[-1]
    assert isinstance(last_msg, SystemMessage)
    assert last_msg.additional_kwargs.get("compaction") is True
    assert last_msg.additional_kwargs.get("compaction_event") == "pre_tier3_memory_flush"
