"""Property-based test: ``compaction_pre_model_hook`` output is a valid LLM conversation.

Hypothesis generates varied well-formed conversation shapes and asserts that
the three-region projection returned by the hook still passes
``LLMConversationShapeValidator`` — regardless of whether summarisation fires.

Invariants the property pins down:
  * No orphan ``ToolMessage`` in the projection.
  * Every ``AIMessage.tool_calls`` is followed by matching ``ToolMessage``s.
  * ``SystemMessage``s only at the head.
  * Pre-existing regressions from PR #80 (keep-window orphan alignment on
    both first and second firings) stay fixed.
"""

from __future__ import annotations

from typing import Any, Callable
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from executor.compaction.pre_model_hook import compaction_pre_model_hook
from executor.compaction.summarizer import SummarizeResult
from tests.shape_validator import assert_valid_shape


# ---------------------------------------------------------------------------
# Strategies — well-formed conversations
# ---------------------------------------------------------------------------


def _ai_with_tools(turn_idx: int, n_calls: int) -> AIMessage:
    return AIMessage(
        content=f"Step {turn_idx}",
        tool_calls=[
            {
                "id": f"call_{turn_idx}_{i}",
                "name": f"tool_{turn_idx}_{i}",
                "args": {"content": "x" * 10},
                "type": "tool_call",
            }
            for i in range(n_calls)
        ],
    )


def _tool_results_for(ai: AIMessage) -> list[ToolMessage]:
    return [
        ToolMessage(
            content=f"result_{c['id']}",
            tool_call_id=c["id"],
            name=c["name"],
        )
        for c in (ai.tool_calls or [])
    ]


@st.composite
def well_formed_conversations(draw, min_turns: int = 3, max_turns: int = 12):
    """Generate a well-formed LangChain conversation.

    Always starts with a HumanMessage and guarantees ``assert_valid_shape``
    passes on the generated list — the property verifies the hook
    *preserves* validity, not that it can fix a pre-broken input.
    """
    messages: list[BaseMessage] = [HumanMessage(content="start")]
    n_turns = draw(st.integers(min_value=min_turns, max_value=max_turns))

    for turn in range(n_turns):
        kind = draw(
            st.sampled_from(
                ["tool_use", "tool_use", "tool_use", "reasoning", "human"]
            )
        )
        if kind == "tool_use":
            n_calls = draw(st.integers(min_value=1, max_value=3))
            ai = _ai_with_tools(turn, n_calls)
            messages.append(ai)
            messages.extend(_tool_results_for(ai))
        elif kind == "reasoning":
            messages.append(AIMessage(content=f"thinking step {turn}"))
        elif kind == "human":
            messages.append(HumanMessage(content=f"user follow-up {turn}"))
            messages.append(AIMessage(content=f"ack {turn}"))

    return messages


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fresh_state(messages: list[BaseMessage]) -> dict[str, Any]:
    return {
        "messages": messages,
        "summary": "",
        "summarized_through_turn_index": 0,
        "memory_flush_fired_this_task": False,
        "last_super_step_message_count": 0,
        "tier3_firings_count": 0,
        "tier3_fatal_short_circuited": False,
    }


def _post_first_firing_state(
    messages: list[BaseMessage], summarized_through: int
) -> dict[str, Any]:
    """State after one prior firing — summary non-empty, watermark advanced."""
    return {
        "messages": messages,
        "summary": "PRIOR SUMMARY.",
        "summarized_through_turn_index": summarized_through,
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
        summary_text="NEW SUMMARY",
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
# Property: projection preserves conversation-shape validity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@given(
    messages=well_formed_conversations(),
    # Straddle the trigger fraction so the property covers both no-fire
    # and fire paths with identical invariants.
    token_count=st.sampled_from([1_000, 6_000, 9_000]),
    prior_firing=st.booleans(),
    prior_summarized_through=st.integers(min_value=1, max_value=6),
)
@settings(
    max_examples=120,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
async def test_pre_model_hook_preserves_shape(
    messages: list[BaseMessage],
    token_count: int,
    prior_firing: bool,
    prior_summarized_through: int,
):
    """For any well-formed input + any trigger-straddling token count + either
    fresh-state or post-first-firing state, the projection is a valid LLM
    conversation.
    """
    # Sanity: generated conversation is already valid.
    assert_valid_shape(messages)

    if prior_firing:
        clamped = min(prior_summarized_through, max(1, len(messages) // 2))
        state = _post_first_firing_state(messages, summarized_through=clamped)
    else:
        state = _fresh_state(messages)

    # 10_000-token context window → 0.85 trigger ≈ 8_500. Sampled tokens
    # include values below (1_000, 6_000) and above (9_000) the trigger.
    result = await compaction_pre_model_hook(
        raw_messages=messages,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_summarizer(),
        estimate_tokens_fn=_fixed_estimator(token_count),
        system_prompt="You are a test agent.",
    )

    # The property: regardless of whether the summariser fired, the projection
    # is a valid conversation that all major providers accept.
    assert_valid_shape(result.messages)

    # Additional structural check: SystemMessages are only at the head.
    projection = result.messages
    first_non_system_idx = next(
        (i for i, m in enumerate(projection) if not isinstance(m, SystemMessage)),
        len(projection),
    )
    # No SystemMessage may appear after the first non-SystemMessage.
    for m in projection[first_non_system_idx:]:
        assert not isinstance(m, SystemMessage), (
            "SystemMessages must appear only at the head of the projection."
        )
