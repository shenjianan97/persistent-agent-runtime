"""Regression: Tier 3 summarization must not leave an orphan ToolMessage at tail head.

Bug reproduced from production task 3b8d422f-ffe8-4b4e-ae8a-f65750860b95:

When the natural ``tool_positions[-KEEP_TOOL_USES]`` boundary lands on a
ToolMessage, the AIMessage that issued the matching tool_use is included in
the summarized slice and replaced by the summary SystemMessage. The tail then
begins with a ToolMessage (orphan toolResult), and Bedrock's Converse API
rejects the request with::

    ValidationException: Expected toolResult blocks at messages.0.content
    for the following Ids: tooluse_...

Invariant under test:
    After Tier 3 fires, the first non-SystemMessage in the compacted view must
    be either an AIMessage or a HumanMessage — never a ToolMessage.
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


def _base_state() -> dict[str, Any]:
    return {
        "cleared_through_turn_index": 0,
        "truncated_args_through_turn_index": 0,
        "summarized_through_turn_index": 0,
        "summary_marker": "",
        "memory_flush_fired_this_task": False,
        "last_super_step_message_count": 0,
        "tier3_firings_count": 0,
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
        summary_text="SUMMARY",
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
async def test_tier3_tail_does_not_start_with_orphan_tool_message():
    """Regression: tail head must be AIMessage/HumanMessage, never a bare ToolMessage.

    Messages layout (KEEP_TOOL_USES=3):
        idx 0:  HumanMessage
        idx 1:  AIMessage(call_0)
        idx 2:  ToolMessage(call_0)
        ...
        idx 2k+1: AIMessage(call_k)
        idx 2k+2: ToolMessage(call_k)

    tool_positions[-3] lands on a ToolMessage, so the naive slice trims the
    AIMessage that paired with it. The tail's first message is then a
    toolResult whose tool_use is gone — Bedrock rejects the request.
    """
    msgs = _make_messages(n=6)  # 6 pairs; 6 ToolMessages total
    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_summarizer(),
        estimate_tokens_fn=_fixed_estimator(8_000),  # above tier3 threshold
    )

    assert any(isinstance(e, Tier3FiredEvent) for e in result.events), (
        "Tier 3 must fire in this scenario"
    )

    compacted = result.messages
    # Drop any leading SystemMessage (the summary marker).
    tail = [m for m in compacted if not isinstance(m, SystemMessage)]
    assert tail, "tail must be non-empty"

    first = tail[0]
    assert not isinstance(first, ToolMessage), (
        f"tail must not begin with an orphan ToolMessage "
        f"(call_id={getattr(first, 'tool_call_id', None)}). "
        "Tier 3 summarization cut between an AIMessage and its ToolMessage, "
        "which breaks Bedrock Converse's tool_use/toolResult pairing."
    )

    # Stronger, provider-agnostic invariant: the full compacted message list
    # must pass the LLMConversationShapeValidator. This catches the orphan
    # ToolMessage bug plus any future tool_use/tool_result mismatches that
    # would be rejected by Bedrock, Anthropic, OpenAI, or Gemini.
    assert_valid_shape(compacted)
