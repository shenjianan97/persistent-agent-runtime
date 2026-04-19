"""Exclude-tools invariant test (Track 7 AC 6).

AC 6: ``exclude_tools`` entries (platform + agent) are never masked by Tier 1
regardless of the message's age relative to the protection window.

Design doc: docs/design-docs/phase-2/track-7-context-window-management.md
§Tier 1: tool-result clearing — "Exclude list".

The platform exclude list includes ``memory_note``, ``save_memory``,
``request_human_input``, ``memory_search``, ``task_history_get``.  Agent-level
``exclude_tools`` extend this set via union (not override).
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

from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS
from executor.compaction.pipeline import (
    Tier1AppliedEvent,
    compact_for_llm,
)
from executor.compaction.summarizer import SummarizeResult
from executor.compaction.thresholds import resolve_thresholds
from executor.compaction.transforms import clear_tool_results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_pair(
    i: int,
    tool_name: str = "some_tool",
    content: str = "result",
    arg_content: str = "x" * 10,
) -> list[BaseMessage]:
    call_id = f"call_{i}"
    return [
        AIMessage(
            content=f"step {i}",
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


def _agent_config(exclude_tools: list[str] | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "provider": "other",
        "model": "test-model",
        "context_management": {
            "exclude_tools": exclude_tools or [],
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


def _make_successful_summarizer() -> AsyncMock:
    mock = AsyncMock()
    mock.return_value = SummarizeResult(
        summary_text="Summary.",
        skipped=False,
        skipped_reason=None,
        summarizer_model_id="test-model",
        tokens_in=100,
        tokens_out=50,
        cost_microdollars=0,
        latency_ms=10,
    )
    return mock


def _get_tool_message(messages: list[BaseMessage], tool_name: str) -> list[ToolMessage]:
    """Return all ToolMessages for a given tool name."""
    return [
        m for m in messages
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == tool_name
    ]


PLACEHOLDER_PREFIX = "[tool output not retained —"


def _is_placeholder(content: str) -> bool:
    return content.startswith(PLACEHOLDER_PREFIX)


def _is_original(content: str, original: str) -> bool:
    return content == original


# ---------------------------------------------------------------------------
# Test: platform-excluded tools never masked (memory_note)
# ---------------------------------------------------------------------------


def test_memory_note_never_cleared_by_tier1():
    """memory_note is in PLATFORM_EXCLUDE_TOOLS and must never be masked.

    Build a history with many old memory_note results mixed with regular
    tool results. Force Tier 1 to fire (many old messages). Verify every
    memory_note ToolMessage retains its original content.
    """
    MEMORY_NOTE_CONTENT = "Important agent observation for future tasks."
    REGULAR_CONTENT = "r" * 500  # large clearable content
    KEEP = 3

    # Build: 20 pairs alternating between memory_note and web_search
    msgs: list[BaseMessage] = [HumanMessage(content="task")]
    for i in range(20):
        tool_name = "memory_note" if i % 2 == 0 else "web_search"
        content = MEMORY_NOTE_CONTENT if tool_name == "memory_note" else REGULAR_CONTENT
        msgs.extend(_tool_pair(i, tool_name=tool_name, content=content))

    # Run Tier 1 directly with all messages being older than the keep window
    result = clear_tool_results(
        messages=msgs,
        cleared_through_turn_index=0,
        keep=KEEP,
        exclude_tools_effective=PLATFORM_EXCLUDE_TOOLS,
    )

    compacted = result.messages

    # All memory_note ToolMessages must retain original content
    memory_note_msgs = _get_tool_message(compacted, "memory_note")
    assert memory_note_msgs, "Expected memory_note tool messages in compacted result"
    for m in memory_note_msgs:
        assert _is_original(m.content, MEMORY_NOTE_CONTENT), (
            f"memory_note ToolMessage was incorrectly cleared: {m.content[:80]}"
        )

    # Old web_search messages (outside keep window) should be cleared
    # (We just check some were cleared — the details are covered in transform tests)
    web_search_msgs = _get_tool_message(compacted, "web_search")
    old_web_search = [m for m in web_search_msgs if _is_placeholder(m.content)]
    assert old_web_search, "Expected old web_search ToolMessages to be cleared"


# ---------------------------------------------------------------------------
# Test: agent-level exclude_tools union with platform list
# ---------------------------------------------------------------------------


def test_agent_exclude_tools_union_with_platform_list():
    """Agent-level exclude_tools is union with platform list — both are preserved."""
    AGENT_TOOL = "custom_tool_x"
    AGENT_CONTENT = "Custom tool data to preserve."
    PLATFORM_CONTENT = "Memory note data."
    REGULAR_CONTENT = "r" * 500
    KEEP = 3

    msgs: list[BaseMessage] = [HumanMessage(content="task")]
    for i in range(20):
        if i % 3 == 0:
            tool_name = AGENT_TOOL
            content = AGENT_CONTENT
        elif i % 3 == 1:
            tool_name = "memory_note"
            content = PLATFORM_CONTENT
        else:
            tool_name = "web_search"
            content = REGULAR_CONTENT
        msgs.extend(_tool_pair(i, tool_name=tool_name, content=content))

    # Union: both agent tool and platform tools excluded
    exclude_effective = PLATFORM_EXCLUDE_TOOLS | frozenset({AGENT_TOOL})

    result = clear_tool_results(
        messages=msgs,
        cleared_through_turn_index=0,
        keep=KEEP,
        exclude_tools_effective=exclude_effective,
    )

    compacted = result.messages

    # Agent-excluded tool must retain content
    agent_tool_msgs = _get_tool_message(compacted, AGENT_TOOL)
    for m in agent_tool_msgs:
        assert _is_original(m.content, AGENT_CONTENT), (
            f"Agent-excluded tool {AGENT_TOOL!r} was incorrectly cleared: {m.content[:80]}"
        )

    # Platform-excluded tool must retain content
    memory_note_msgs = _get_tool_message(compacted, "memory_note")
    for m in memory_note_msgs:
        assert _is_original(m.content, PLATFORM_CONTENT), (
            f"Platform-excluded tool memory_note was incorrectly cleared: {m.content[:80]}"
        )


# ---------------------------------------------------------------------------
# Test: pipeline respects exclude_tools end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_exclude_tools_never_cleared_by_tier1():
    """End-to-end pipeline: exclude_tools results are never cleared by Tier 1.

    Constructs a realistic history with memory_note and web_search results.
    Forces Tier 1 above threshold. Asserts memory_note content is preserved.
    """
    MEMORY_NOTE_CONTENT = "Important persistent note."
    WEB_SEARCH_CONTENT = "s" * 800  # large to fill context

    msgs: list[BaseMessage] = [HumanMessage(content="Perform some research.")]
    for i in range(20):
        tool_name = "memory_note" if i % 3 == 0 else "web_search"
        content = MEMORY_NOTE_CONTENT if tool_name == "memory_note" else WEB_SEARCH_CONTENT
        msgs.extend(_tool_pair(i, tool_name=tool_name, content=content))

    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    # Above Tier 1 threshold but below Tier 3
    token_count = thresholds.tier1 + 500

    # Inject a decrementing estimator so Tier 3 does not fire
    call_count = [0]
    def estimator(messages: list[BaseMessage]) -> int:
        call_count[0] += 1
        if call_count[0] == 1:
            return thresholds.tier1 + 500  # trigger Tier 1
        return thresholds.tier3 - 100     # after Tier 1, below Tier 3

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_base_state(),
        agent_config=_agent_config(),  # no extra exclude_tools
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=estimator,
    )

    # Tier 1 must have fired
    tier1_events = [e for e in result.events if isinstance(e, Tier1AppliedEvent)]
    assert tier1_events, "Tier 1 must have fired for this test to be meaningful"

    # All memory_note ToolMessages must still have their original content
    memory_note_msgs = _get_tool_message(result.messages, "memory_note")
    assert memory_note_msgs, "Expected memory_note messages in compacted result"
    for m in memory_note_msgs:
        assert _is_original(m.content, MEMORY_NOTE_CONTENT), (
            f"memory_note ToolMessage cleared in pipeline (AC 6 violated): {m.content[:80]}"
        )


# ---------------------------------------------------------------------------
# Test: agent-level exclude_tools via pipeline config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_agent_exclude_tools_via_config():
    """Agent-level exclude_tools passed via agent_config are respected by pipeline."""
    AGENT_TOOL = "my_special_tool"
    SPECIAL_CONTENT = "This must never be cleared."
    REGULAR_CONTENT = "r" * 800

    msgs: list[BaseMessage] = [HumanMessage(content="task")]
    for i in range(15):
        tool_name = AGENT_TOOL if i % 4 == 0 else "web_search"
        content = SPECIAL_CONTENT if tool_name == AGENT_TOOL else REGULAR_CONTENT
        msgs.extend(_tool_pair(i, tool_name=tool_name, content=content))

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
        agent_config=_agent_config(exclude_tools=[AGENT_TOOL]),
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_make_successful_summarizer(),
        estimate_tokens_fn=estimator,
    )

    tier1_events = [e for e in result.events if isinstance(e, Tier1AppliedEvent)]
    assert tier1_events, "Tier 1 must have fired for this test to be meaningful"

    agent_tool_msgs = _get_tool_message(result.messages, AGENT_TOOL)
    assert agent_tool_msgs, "Expected agent-excluded tool messages in output"
    for m in agent_tool_msgs:
        assert _is_original(m.content, SPECIAL_CONTENT), (
            f"Agent-excluded tool {AGENT_TOOL!r} was incorrectly cleared in pipeline: "
            f"{m.content[:80]}"
        )


# ---------------------------------------------------------------------------
# Test: platform PLATFORM_EXCLUDE_TOOLS contains expected tools
# ---------------------------------------------------------------------------


def test_platform_exclude_tools_contains_required_tools():
    """PLATFORM_EXCLUDE_TOOLS must include the five required tools from the design doc."""
    required = {
        "memory_note",
        "save_memory",
        "request_human_input",
        "memory_search",
        "task_history_get",
    }
    assert required.issubset(PLATFORM_EXCLUDE_TOOLS), (
        f"PLATFORM_EXCLUDE_TOOLS is missing required entries: "
        f"{required - PLATFORM_EXCLUDE_TOOLS}"
    )
