"""Regression: second Tier 3 firing must not strand an orphan ToolMessage.

Bug reproduced from production task f564d8cc-4c85-4d05-8c2b-967d7f043615:

On the *second* tier3 firing, ``state.summary_marker`` is non-empty, so
``compact_for_llm`` prepends a SystemMessage at index 0 (pipeline.py:326).
``tool_positions`` / ``protect_from_index`` / ``new_summarized_through`` are
then computed in the WITH-prepend indexing. But the tail-rebuild strips
compaction SystemMessages BEFORE slicing at ``new_summarized_through``:

    tail = [m for m in messages if not <compaction SystemMessage>][new_summarized_through:]

The strip removes the prepend, but the slice index still carries the +1
offset from the prepended SystemMessage. Result: the tail skips past the
aligned AIMessage boundary by one position and starts with a bare
ToolMessage — the same orphan-tool-result shape Bedrock rejects with::

    ValidationException: Expected toolResult blocks at messages.0.content
    for the following Ids: tooluse_2STsAN8Wd0DerbejW9TxpJ
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

from executor.compaction.pipeline import Tier3FiredEvent, compact_for_llm
from executor.compaction.summarizer import SummarizeResult
from tests.shape_validator import assert_valid_shape


def _tool_pair(i: int) -> list[BaseMessage]:
    call_id = f"call_{i}"
    return [
        AIMessage(
            content=f"Step {i}",
            tool_calls=[{
                "id": call_id,
                "name": f"tool_{i}",
                "args": {},
                "type": "tool_call",
            }],
        ),
        ToolMessage(content="result", tool_call_id=call_id, name=f"tool_{i}"),
    ]


def _make_messages(n: int) -> list[BaseMessage]:
    msgs: list[BaseMessage] = [HumanMessage(content="task input")]
    for i in range(n):
        msgs.extend(_tool_pair(i))
    return msgs


def _state_with_prior_summary(summarized_through: int) -> dict[str, Any]:
    """State representing a task that already had one Tier 3 firing.

    ``summary_marker`` non-empty → next ``compact_for_llm`` call prepends
    a SystemMessage at index 0, which is the shift that breaks the
    boundary math on the second firing.
    """
    return {
        "cleared_through_turn_index": summarized_through,
        "truncated_args_through_turn_index": summarized_through,
        "summarized_through_turn_index": summarized_through,
        "summary_marker": "PRIOR SUMMARY of earlier steps.\n",
        "memory_flush_fired_this_task": False,
        "last_super_step_message_count": 0,
        "tier3_firings_count": 1,
        "tier3_fatal_short_circuited": False,
    }


def _agent_config() -> dict[str, Any]:
    return {
        "provider": "other",
        "model": "test-model",
        "context_management": {},
    }


def _task_context() -> dict[str, Any]:
    return {
        "tenant_id": "tenant-1",
        "agent_id": "agent-1",
        "task_id": "task-1",
        "checkpoint_id": None,
        "cost_ledger": None,
        "callbacks": [],
    }


def _fixed_estimator(tokens: int) -> Callable[[list[BaseMessage]], int]:
    def _e(_messages: list[BaseMessage]) -> int:
        return tokens

    return _e


def _summarizer() -> AsyncMock:
    mock = AsyncMock()
    mock.return_value = SummarizeResult(
        summary_text="SECOND SUMMARY",
        skipped=False,
        skipped_reason=None,
        summarizer_model_id="test-model",
        tokens_in=100,
        tokens_out=50,
        cost_microdollars=0,
        latency_ms=10,
    )
    return mock


@pytest.mark.asyncio
async def test_second_tier3_firing_preserves_tool_use_pairing():
    """Regression: when ``summary_marker`` is non-empty (i.e. Tier 3 already
    fired once), a subsequent Tier 3 firing must not produce an orphan
    ToolMessage at tail head.

    Setup: 10 tool-use pairs + a non-empty ``summary_marker`` simulating a
    prior firing. token estimate forces Tier 3 to fire again.
    """
    msgs = _make_messages(n=10)

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_state_with_prior_summary(summarized_through=3),
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_summarizer(),
        estimate_tokens_fn=_fixed_estimator(8_000),  # above tier3 threshold
    )

    assert any(isinstance(e, Tier3FiredEvent) for e in result.events), (
        "Tier 3 must fire a second time in this scenario"
    )

    # Provider-agnostic invariant — same validator used on all pipeline tests.
    assert_valid_shape(result.messages)
