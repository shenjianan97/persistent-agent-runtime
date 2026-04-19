"""Pre-Tier-3 memory flush redrive safety test (Track 7 AC 7 redrive).

AC 7 (redrive portion): A follow-up or redrive that resumes from a
post-flush checkpoint must restore the ``memory_flush_fired_this_task=True``
flag from state, which causes the flush to NOT re-fire on the redriven task.

This is a checkpoint-restore scenario:
  1. First run: flush fires → ``memory_flush_fired_this_task=True`` written.
  2. Simulate checkpoint save by reading ``state_updates`` from result.
  3. Redrive: restore state from the saved checkpoint (flag=True).
  4. Assert: flush does NOT re-fire on the redriven call.
  5. Assert: Tier 3 proceeds normally after the restored flag.

Design doc: docs/design-docs/phase-2/track-7-context-window-management.md
§Pre-Tier-3 memory flush — "Follow-up / redrive interaction".
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


def _agent_config_with_memory(**overrides) -> dict[str, Any]:
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


def _task_context(**overrides) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "tenant_id": "tenant-1",
        "agent_id": "agent-1",
        "task_id": "task-redrive-1",
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
# Core redrive-safety scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redrive_from_post_flush_checkpoint_does_not_refire():
    """Redrive from post-flush checkpoint: flush does NOT re-fire.

    Scenario:
    1. Call 1: flush conditions met → MemoryFlushFiredEvent emitted,
       state_updates['memory_flush_fired_this_task'] = True.
    2. Simulate checkpoint save: build redrived state using call 1's state_updates.
    3. Call 2 (redrive): memory_flush_fired_this_task=True restored from checkpoint.
    4. Assert: flush does NOT fire on call 2.
    5. Assert: Tier 3 fires normally on call 2 (flush flag does not block Tier 3).
    """
    msgs = _make_messages(5, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    # Call 1: flush should fire
    state_before_flush = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,  # new work landed
    )
    summarizer_1 = _make_successful_summarizer("SUMMARY_1")

    result_1 = await compact_for_llm(
        raw_messages=msgs,
        state=state_before_flush,
        agent_config=_agent_config_with_memory(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer_1,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events_1 = [e for e in result_1.events if isinstance(e, MemoryFlushFiredEvent)]
    assert flush_events_1, (
        "Flush must have fired on call 1 (prerequisite for this test to be meaningful)"
    )
    assert result_1.state_updates.get("memory_flush_fired_this_task") is True, (
        "state_updates must set memory_flush_fired_this_task=True when flush fires"
    )

    # --- Checkpoint save: build redrived state from call 1's state_updates ---
    # Merge state_before_flush with state_updates from call 1
    checkpoint_state = {**state_before_flush, **result_1.state_updates}

    # Call 2 (redrive): restore from checkpoint where flag=True
    summarizer_2 = _make_successful_summarizer("SUMMARY_2")

    result_2 = await compact_for_llm(
        raw_messages=msgs,
        state=checkpoint_state,  # restored from checkpoint
        agent_config=_agent_config_with_memory(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer_2,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    # Flush must NOT re-fire on the redriven call
    flush_events_2 = [e for e in result_2.events if isinstance(e, MemoryFlushFiredEvent)]
    assert not flush_events_2, (
        "MemoryFlushFiredEvent must NOT fire on redrive when checkpoint restores "
        "memory_flush_fired_this_task=True (AC 7 redrive safety)"
    )

    # Tier 3 MUST fire normally after flush already fired
    tier3_events_2 = [e for e in result_2.events if isinstance(e, Tier3FiredEvent)]
    assert tier3_events_2, (
        "Tier 3 must fire normally on the redrived call after flush has already fired. "
        "The flush flag must not block Tier 3 on subsequent calls."
    )


@pytest.mark.asyncio
async def test_redrive_from_pre_flush_checkpoint_does_fire_flush():
    """Redrive from pre-flush checkpoint: flush fires again on the redriven task.

    If the checkpoint was saved BEFORE the flush fired (flag=False), the
    redriven task should fire the flush (as if no flush had occurred).
    This verifies the flag is authoritative — False in checkpoint → flush fires.
    """
    msgs = _make_messages(5, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    # Simulate checkpoint where flag was NOT yet set (pre-flush)
    pre_flush_checkpoint_state = _base_state(
        memory_flush_fired_this_task=False,  # not yet fired at checkpoint time
        last_super_step_message_count=len(msgs) - 1,  # new work landed
    )

    summarizer = _make_successful_summarizer("SUMMARY")
    result = await compact_for_llm(
        raw_messages=msgs,
        state=pre_flush_checkpoint_state,
        agent_config=_agent_config_with_memory(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_events = [e for e in result.events if isinstance(e, MemoryFlushFiredEvent)]
    assert flush_events, (
        "Flush must fire when restoring from pre-flush checkpoint (flag=False)"
    )


@pytest.mark.asyncio
async def test_flush_fires_exactly_once_across_redrive_cycle():
    """Over a complete flush + redrive cycle, the flush fires exactly once.

    - Phase 1: flag=False → flush fires.
    - Phase 2: restore flag=True from state_updates → flush does NOT re-fire.
    Total MemoryFlushFiredEvent count across both phases: exactly 1.
    """
    msgs = _make_messages(5, content_size=200)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    token_count = thresholds.tier3 + 1000

    state_phase1 = _base_state(
        memory_flush_fired_this_task=False,
        last_super_step_message_count=len(msgs) - 1,
    )
    r1 = await compact_for_llm(
        raw_messages=msgs,
        state=state_phase1,
        agent_config=_agent_config_with_memory(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer("SUMMARY_PHASE1"),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_count_phase1 = sum(
        1 for e in r1.events if isinstance(e, MemoryFlushFiredEvent)
    )
    assert flush_count_phase1 == 1, "Flush must fire exactly once in phase 1"

    # Build checkpoint state from phase 1's updates
    state_phase2 = {**state_phase1, **r1.state_updates}

    r2 = await compact_for_llm(
        raw_messages=msgs,
        state=state_phase2,
        agent_config=_agent_config_with_memory(),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer("SUMMARY_PHASE2"),
        estimate_tokens_fn=_fixed_token_estimate(token_count),
    )

    flush_count_phase2 = sum(
        1 for e in r2.events if isinstance(e, MemoryFlushFiredEvent)
    )
    assert flush_count_phase2 == 0, "Flush must NOT re-fire in phase 2 (post-redrive)"

    total_flush_count = flush_count_phase1 + flush_count_phase2
    assert total_flush_count == 1, (
        f"Flush must fire exactly once over the full cycle, "
        f"but fired {total_flush_count} times"
    )
