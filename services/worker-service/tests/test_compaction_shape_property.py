"""Property-based test: compact_for_llm output is a valid LLM conversation.

Hypothesis generates varied well-formed conversation shapes and asserts that
after ``compact_for_llm`` runs (including Tier 1 / 1.5 / 3), the output still
passes ``LLMConversationShapeValidator``.

This is the companion to ``test_compaction_tier3_tool_boundary.py`` — the
hand-rolled regression pins down the original bug; this property test
sweeps the shape space to catch *future* boundary bugs before they reach
production.

Why property tests here:
- The input domain (conversation shapes) has many dimensions: number of AI
  turns, tool_call count per AI turn, presence of reasoning-only AI turns,
  HumanMessage interjections, token-estimator values straddling each tier
  threshold.
- The invariant is crisp: "validator passes". Hard to express as a handful
  of parameterized cases; easy to express as a property.
- Shrinking (Hypothesis's superpower) will report the *smallest* shape that
  breaks — much cheaper to debug than a 30-message reproduction.
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
    ToolMessage,
)

from executor.compaction.pipeline import compact_for_llm
from executor.compaction.summarizer import SummarizeResult
from tests.shape_validator import assert_valid_shape


# ---------------------------------------------------------------------------
# Strategies: build well-formed conversations with varied shapes
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

    Each 'turn' is one of:
      - tool-use turn: AIMessage with 1-3 tool_calls + matching ToolMessages
      - reasoning turn: AIMessage with text, no tool_calls
      - human interjection: HumanMessage followed by a required AI turn

    Always starts with a HumanMessage. The generator guarantees
    ``assert_valid_shape`` passes on the generated list — we want to verify
    that ``compact_for_llm`` *preserves* validity, not that it can fix a
    pre-broken input.
    """
    messages: list[BaseMessage] = [HumanMessage(content="start")]
    n_turns = draw(st.integers(min_value=min_turns, max_value=max_turns))

    for turn in range(n_turns):
        # Choose turn kind. Weight toward tool-use turns since those are
        # where tier 3 boundaries are most at risk.
        kind = draw(st.sampled_from(["tool_use", "tool_use", "tool_use", "reasoning", "human"]))

        if kind == "tool_use":
            n_calls = draw(st.integers(min_value=1, max_value=3))
            ai = _ai_with_tools(turn, n_calls)
            messages.append(ai)
            messages.extend(_tool_results_for(ai))
        elif kind == "reasoning":
            messages.append(AIMessage(content=f"thinking step {turn}"))
        elif kind == "human":
            messages.append(HumanMessage(content=f"user follow-up {turn}"))
            # Force a responding AI turn so the conversation can't end on a
            # bare HumanMessage mid-sequence (valid, but less interesting).
            messages.append(AIMessage(content=f"ack {turn}"))

    return messages


# ---------------------------------------------------------------------------
# Fixtures mirroring other compaction tests
# ---------------------------------------------------------------------------


def _fresh_state() -> dict[str, Any]:
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


def _post_first_firing_state(summarized_through: int) -> dict[str, Any]:
    """State representing a task that already had one Tier 3 firing.

    ``summary_marker`` is non-empty → the next ``compact_for_llm`` call
    prepends a SystemMessage at index 0. This shift is what stranded
    tail ToolMessages off-by-one on second firings before the fix.
    """
    return {
        "cleared_through_turn_index": summarized_through,
        "truncated_args_through_turn_index": summarized_through,
        "summarized_through_turn_index": summarized_through,
        "summary_marker": "PRIOR SUMMARY.\n",
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


# ---------------------------------------------------------------------------
# Property: compact_for_llm preserves conversation-shape validity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@given(
    messages=well_formed_conversations(),
    # Token count straddling each tier threshold: 1_000 (below tier 1),
    # 6_000 (tier 1 zone), 8_000 (tier 3 zone). The 10K window yields
    # roughly tier1≈3K and tier3≈6K under resolve_thresholds.
    token_count=st.sampled_from([1_000, 6_000, 8_000]),
    # Prior-firing state exercises the second-firing code path where a
    # compaction SystemMessage is prepended at index 0. Production bug
    # f564d8cc-4c85-4d05-8c2b-967d7f043615 surfaced only in this path.
    prior_firing=st.booleans(),
    prior_summarized_through=st.integers(min_value=1, max_value=6),
)
@settings(
    max_examples=120,
    # Async + Hypothesis interact with pytest-asyncio's function-scoped event
    # loop; suppressing this check keeps the property contained to shape
    # preservation without the function-scoped-fixture warning.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
async def test_compact_for_llm_preserves_shape(
    messages: list[BaseMessage],
    token_count: int,
    prior_firing: bool,
    prior_summarized_through: int,
):
    """Property: for any well-formed input + any tier-triggering token count
    + either fresh-state or post-first-firing state, the compacted output
    is a valid LLM conversation."""
    # Sanity: precondition — the input itself is already valid. If this
    # trips, the generator is wrong, not the pipeline.
    assert_valid_shape(messages)

    # Clamp prior_summarized_through so it doesn't run past the generated
    # conversation; otherwise tier3 would summarize an empty slice.
    if prior_firing:
        clamped = min(prior_summarized_through, max(1, len(messages) // 2))
        state = _post_first_firing_state(summarized_through=clamped)
    else:
        state = _fresh_state()

    result = await compact_for_llm(
        raw_messages=messages,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_summarizer(),
        estimate_tokens_fn=_fixed_estimator(token_count),
    )

    # The actual property: regardless of which tiers fired, the output
    # is still a valid conversation that all major providers accept.
    assert_valid_shape(result.messages)
