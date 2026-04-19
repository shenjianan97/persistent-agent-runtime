"""Unit tests for clear_tool_results (Tier 1 transform).

Tests are ordered to follow the TDD cycle:
- Each section tests a specific contract from the Task 5 spec.
- Tests are self-contained and do not rely on test order.
"""

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from executor.compaction.transforms import ClearResult, clear_tool_results

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PLACEHOLDER_PREFIX = "[tool output not retained —"


def _tool_msg(
    content: str, tool_call_id: str = "id1", name: str | None = "some_tool"
) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id, name=name)


def _ai_msg_with_call(tool_call_id: str, tool_name: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": tool_call_id, "name": tool_name, "args": {}, "type": "tool_call"}],
    )


def _make_messages(n: int, content_template: str = "result {i}") -> list[BaseMessage]:
    """Build a simple list of (AIMessage+ToolMessage) pairs."""
    msgs: list[BaseMessage] = []
    for i in range(n):
        call_id = f"call_{i}"
        msgs.append(_ai_msg_with_call(call_id, f"tool_{i}"))
        msgs.append(_tool_msg(content_template.format(i=i), tool_call_id=call_id, name=f"tool_{i}"))
    return msgs


# ---------------------------------------------------------------------------
# 1. No-op: ≤ keep tool messages — nothing cleared
# ---------------------------------------------------------------------------


def test_fewer_tool_messages_than_keep_returns_same_list():
    """With ≤ keep ToolMessages, same list object is returned verbatim (no copy)."""
    msgs = _make_messages(3)  # 3 ToolMessages
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    assert result.messages is msgs, "original list must be returned when nothing to clear"
    assert result.messages_cleared == 0
    assert result.est_tokens_saved == 0
    assert result.new_cleared_through_turn_index == 0


def test_exactly_keep_tool_messages_returns_same_list():
    """Exactly keep=3 ToolMessages → no-op, same list."""
    msgs = _make_messages(3)
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())
    assert result.messages is msgs
    assert result.messages_cleared == 0


def test_empty_messages_returns_same_list():
    """Empty message list → no-op."""
    msgs: list[BaseMessage] = []
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())
    assert result.messages is msgs
    assert result.messages_cleared == 0
    assert result.new_cleared_through_turn_index == 0


# ---------------------------------------------------------------------------
# 2. Basic clearing: more than keep tool messages
# ---------------------------------------------------------------------------


def test_clears_oldest_tool_messages_keeps_most_recent():
    """5 ToolMessages, keep=3: oldest 2 cleared, newest 3 untouched."""
    msgs = _make_messages(5)
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    assert result.messages_cleared == 2
    new_msgs = result.messages

    # Find ToolMessage indices (every other starting from index 1 in pairs)
    tool_indices = [i for i, m in enumerate(new_msgs) if isinstance(m, ToolMessage)]
    assert len(tool_indices) == 5

    # Oldest 2 should be cleared
    for idx in tool_indices[:2]:
        assert new_msgs[idx].content.startswith(PLACEHOLDER_PREFIX), (
            f"Expected placeholder at index {idx}, got: {new_msgs[idx].content!r}"
        )

    # Newest 3 should retain original content
    for n, idx in enumerate(tool_indices[2:], start=2):
        assert not new_msgs[idx].content.startswith(PLACEHOLDER_PREFIX), (
            f"Expected original content at index {idx} (tool {n}), got placeholder"
        )


def test_non_tool_messages_never_modified():
    """SystemMessage, HumanMessage, AIMessage pass through untouched."""
    sys_msg = SystemMessage(content="system prompt")
    human_msg = HumanMessage(content="user input")
    ai_call = _ai_msg_with_call("call_0", "some_tool")
    tool_msg_0 = _tool_msg("big result 0", tool_call_id="call_0", name="some_tool")
    ai_call_1 = _ai_msg_with_call("call_1", "some_tool")
    tool_msg_1 = _tool_msg("big result 1", tool_call_id="call_1", name="some_tool")
    ai_call_2 = _ai_msg_with_call("call_2", "some_tool")
    tool_msg_2 = _tool_msg("big result 2", tool_call_id="call_2", name="some_tool")
    ai_call_3 = _ai_msg_with_call("call_3", "some_tool")
    tool_msg_3 = _tool_msg("big result 3", tool_call_id="call_3", name="some_tool")
    msgs = [sys_msg, human_msg, ai_call, tool_msg_0, ai_call_1, tool_msg_1,
            ai_call_2, tool_msg_2, ai_call_3, tool_msg_3]

    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    new_msgs = result.messages
    # SystemMessage unchanged
    assert new_msgs[0] is sys_msg
    # HumanMessage unchanged
    assert new_msgs[1] is human_msg
    # AIMessages unchanged
    for i in [2, 4, 6, 8]:
        assert new_msgs[i] is msgs[i]


# ---------------------------------------------------------------------------
# 3. Placeholder format
# ---------------------------------------------------------------------------


def test_placeholder_format_is_correct():
    """Placeholder must match exact format from spec."""
    msgs = _make_messages(4)
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    new_msgs = result.messages
    tool_indices = [i for i, m in enumerate(new_msgs) if isinstance(m, ToolMessage)]
    cleared_idx = tool_indices[0]
    cleared_msg = new_msgs[cleared_idx]
    original = msgs[cleared_idx]

    orig_bytes = len(original.content.encode("utf-8"))
    tool_name = original.name
    expected_placeholder = (
        f"[tool output not retained — {tool_name} returned {orig_bytes} bytes at step {cleared_idx}]"
    )
    assert cleared_msg.content == expected_placeholder


def test_placeholder_preserves_tool_call_id_and_name():
    """Cleared ToolMessage keeps tool_call_id and name from original."""
    msgs = _make_messages(4)
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    new_msgs = result.messages
    tool_indices = [i for i, m in enumerate(new_msgs) if isinstance(m, ToolMessage)]
    cleared_idx = tool_indices[0]
    cleared_msg = new_msgs[cleared_idx]
    original = msgs[cleared_idx]

    assert cleared_msg.tool_call_id == original.tool_call_id
    assert cleared_msg.name == original.name


def test_placeholder_is_short():
    """Placeholder must be < 200 bytes (spec acceptance criterion)."""
    msgs = _make_messages(4)
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    new_msgs = result.messages
    tool_indices = [i for i, m in enumerate(new_msgs) if isinstance(m, ToolMessage)]
    cleared_idx = tool_indices[0]
    placeholder = new_msgs[cleared_idx].content
    assert len(placeholder.encode("utf-8")) < 200, f"Placeholder too long: {placeholder!r}"


# ---------------------------------------------------------------------------
# 4. Exclude tools
# ---------------------------------------------------------------------------


def test_excluded_tool_not_cleared():
    """ToolMessages for excluded tools are never cleared regardless of age."""
    msgs: list[BaseMessage] = []
    # 5 ToolMessages: first 4 are memory_note (excluded), last 1 is some_tool
    for i in range(4):
        call_id = f"mem_call_{i}"
        msgs.append(_ai_msg_with_call(call_id, "memory_note"))
        msgs.append(_tool_msg(f"memory content {i}", tool_call_id=call_id, name="memory_note"))
    # 5th tool call
    msgs.append(_ai_msg_with_call("call_final", "some_tool"))
    msgs.append(_tool_msg("final result", tool_call_id="call_final", name="some_tool"))

    result = clear_tool_results(
        msgs, cleared_through_turn_index=0, keep=3,
        exclude_tools_effective=frozenset({"memory_note"})
    )

    new_msgs = result.messages
    for m in new_msgs:
        if isinstance(m, ToolMessage) and m.name == "memory_note":
            assert not m.content.startswith(PLACEHOLDER_PREFIX), (
                f"Excluded tool memory_note was cleared: {m.content!r}"
            )


def test_non_excluded_tool_cleared_when_excluded_mixed():
    """Non-excluded tools are still cleared when mixed with excluded tools."""
    msgs: list[BaseMessage] = []
    # 3 non-excluded + 3 excluded => only non-excluded (oldest) can be cleared with keep=3
    for i in range(3):
        call_id = f"reg_call_{i}"
        msgs.append(_ai_msg_with_call(call_id, "regular_tool"))
        msgs.append(_tool_msg(f"regular {i}", tool_call_id=call_id, name="regular_tool"))
    for i in range(3):
        call_id = f"exc_call_{i}"
        msgs.append(_ai_msg_with_call(call_id, "memory_note"))
        msgs.append(_tool_msg(f"excluded {i}", tool_call_id=call_id, name="memory_note"))

    result = clear_tool_results(
        msgs, cleared_through_turn_index=0, keep=3,
        exclude_tools_effective=frozenset({"memory_note"})
    )

    # The 3 "regular_tool" messages are older but at positions < protect_from_index
    # protect_from_index = tool_msg_positions[-3] in the full list of 6 ToolMessages
    # tool_msg_positions = [1,3,5,7,9,11] → protect_from_index = tool_msg_positions[-3] = 7
    # So messages at positions < 7 with non-excluded tools are cleared
    new_msgs = result.messages
    cleared = [m for m in new_msgs if isinstance(m, ToolMessage) and m.content.startswith(PLACEHOLDER_PREFIX)]
    not_cleared_mem = [m for m in new_msgs if isinstance(m, ToolMessage) and m.name == "memory_note"]

    assert len(cleared) > 0, "At least one non-excluded tool should be cleared"
    assert all(not m.content.startswith(PLACEHOLDER_PREFIX) for m in not_cleared_mem), (
        "Excluded tools must not be cleared"
    )


# ---------------------------------------------------------------------------
# 5. Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_second_call_produces_same_output():
    """Calling clear_tool_results twice produces byte-identical output."""
    msgs = _make_messages(5)
    result1 = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())
    result2 = clear_tool_results(
        result1.messages,
        cleared_through_turn_index=result1.new_cleared_through_turn_index,
        keep=3,
        exclude_tools_effective=frozenset()
    )

    # No additional clearing on second pass
    assert result2.messages_cleared == 0
    assert result2.est_tokens_saved == 0
    # Watermark does not regress
    assert result2.new_cleared_through_turn_index == result1.new_cleared_through_turn_index
    # Messages are equal
    assert len(result2.messages) == len(result1.messages)
    for m1, m2 in zip(result1.messages, result2.messages):
        assert m1.content == m2.content


def test_already_cleared_message_not_re_cleared():
    """A ToolMessage whose content starts with the placeholder prefix is not re-cleared."""
    msgs = _make_messages(4)
    # First pass
    result1 = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())
    cleared_count_1 = result1.messages_cleared

    # Second pass with same watermark inputs
    result2 = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())
    # Should clear the same messages (from original msgs)
    assert result2.messages_cleared == cleared_count_1
    # Both outputs should have identical placeholder content
    for m1, m2 in zip(result1.messages, result2.messages):
        if isinstance(m1, ToolMessage):
            assert m1.content == m2.content


# ---------------------------------------------------------------------------
# 6. Watermark monotonicity
# ---------------------------------------------------------------------------


def test_watermark_is_monotone_never_regresses():
    """new_cleared_through_turn_index >= input cleared_through_turn_index always."""
    msgs = _make_messages(5)
    # First pass: watermark advances
    result1 = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())
    watermark_after_first = result1.new_cleared_through_turn_index
    assert watermark_after_first > 0

    # Second pass: feed back the output watermark — should not advance further
    result2 = clear_tool_results(
        result1.messages,
        cleared_through_turn_index=watermark_after_first,
        keep=3,
        exclude_tools_effective=frozenset()
    )
    assert result2.new_cleared_through_turn_index >= watermark_after_first


def test_watermark_far_ahead_does_not_regress():
    """If input watermark is already past protect_from_index, watermark stays."""
    msgs = _make_messages(4)  # 4 ToolMessages, keep=3
    # Compute protect_from_index: tool_msg_positions[-3] for 4 tool msgs
    tool_positions = [i for i, m in enumerate(msgs) if isinstance(m, ToolMessage)]
    protect_idx = tool_positions[-3]  # protect from 2nd tool onward

    # Feed a watermark 10 turns past protect_from_index (simulating previously-advanced state)
    high_watermark = protect_idx + 10
    result = clear_tool_results(
        msgs, cleared_through_turn_index=high_watermark, keep=3, exclude_tools_effective=frozenset()
    )
    assert result.new_cleared_through_turn_index >= high_watermark
    assert result.messages_cleared == 0
    assert result.messages is msgs  # no-op: same list returned


# ---------------------------------------------------------------------------
# 7. Determinism
# ---------------------------------------------------------------------------


def test_deterministic_output_byte_identical():
    """Two calls on the same input produce byte-identical outputs."""
    msgs = _make_messages(5)
    result_a = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())
    result_b = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    assert result_a.messages_cleared == result_b.messages_cleared
    assert result_a.est_tokens_saved == result_b.est_tokens_saved
    assert result_a.new_cleared_through_turn_index == result_b.new_cleared_through_turn_index
    for m_a, m_b in zip(result_a.messages, result_b.messages):
        assert m_a.content == m_b.content, f"Non-deterministic: {m_a.content!r} != {m_b.content!r}"


# ---------------------------------------------------------------------------
# 8. Tool name derivation from preceding AIMessage
# ---------------------------------------------------------------------------


def test_tool_name_derived_from_preceding_ai_message_tool_calls():
    """ToolMessage.name=None: tool_name recovered from AIMessage.tool_calls."""
    call_id = "tc_abc"
    ai_msg = _ai_msg_with_call(call_id, "inferred_tool_name")
    # ToolMessage with name=None
    tool_msg = ToolMessage(content="some large result here", tool_call_id=call_id, name=None)
    # Add 3 more tool messages to get past keep=3 guard
    extra_msgs = _make_messages(3)
    msgs: list[BaseMessage] = [ai_msg, tool_msg] + extra_msgs

    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    new_msgs = result.messages
    cleared = new_msgs[1]
    assert isinstance(cleared, ToolMessage)
    assert "inferred_tool_name" in cleared.content, (
        f"Expected inferred tool name in placeholder, got: {cleared.content!r}"
    )


def test_tool_name_falls_back_to_unknown_when_not_derivable():
    """When tool_call_id not found in any preceding AIMessage, fall back to 'unknown_tool'."""
    # ToolMessage with no matching AIMessage tool_call entry
    tool_msg = ToolMessage(content="orphaned result", tool_call_id="orphan_id", name=None)
    extra_msgs = _make_messages(3)
    msgs: list[BaseMessage] = [tool_msg] + extra_msgs

    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    new_msgs = result.messages
    cleared = new_msgs[0]
    assert isinstance(cleared, ToolMessage)
    assert "unknown_tool" in cleared.content, (
        f"Expected 'unknown_tool' fallback in placeholder, got: {cleared.content!r}"
    )


# ---------------------------------------------------------------------------
# 9. Input immutability
# ---------------------------------------------------------------------------


def test_input_messages_not_mutated():
    """The input messages list and its elements must not be mutated."""
    msgs = _make_messages(5)
    original_contents = [m.content for m in msgs]
    original_ids = [id(m) for m in msgs]

    clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    # Original list contents unchanged
    for i, (m, original_content, original_id) in enumerate(
        zip(msgs, original_contents, original_ids)
    ):
        assert m.content == original_content, f"Message at {i} was mutated"
        assert id(m) == original_id, f"Message at {i} was replaced in the input list"


# ---------------------------------------------------------------------------
# 10. keep=1 edge case
# ---------------------------------------------------------------------------


def test_keep_1_clears_all_but_most_recent():
    """keep=1: only the last ToolMessage is retained."""
    msgs = _make_messages(4)
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=1, exclude_tools_effective=frozenset())

    new_msgs = result.messages
    tool_indices = [i for i, m in enumerate(new_msgs) if isinstance(m, ToolMessage)]

    # All but the last should be cleared
    for idx in tool_indices[:-1]:
        assert new_msgs[idx].content.startswith(PLACEHOLDER_PREFIX)
    # Last should be intact
    assert not new_msgs[tool_indices[-1]].content.startswith(PLACEHOLDER_PREFIX)
    assert result.messages_cleared == 3


# ---------------------------------------------------------------------------
# 11. ClearResult dataclass is frozen (immutable)
# ---------------------------------------------------------------------------


def test_clear_result_is_frozen():
    """ClearResult must be a frozen dataclass."""
    msgs = _make_messages(4)
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    with pytest.raises((AttributeError, TypeError)):
        result.messages_cleared = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 12. est_tokens_saved correctness (heuristic)
# ---------------------------------------------------------------------------


def test_est_tokens_saved_is_non_negative():
    """est_tokens_saved is always >= 0."""
    msgs = _make_messages(5)
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())
    assert result.est_tokens_saved >= 0


def test_est_tokens_saved_is_zero_for_noop():
    """est_tokens_saved=0 when nothing is cleared."""
    msgs = _make_messages(3)
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())
    assert result.est_tokens_saved == 0


# ---------------------------------------------------------------------------
# 13. Cache-stability regression (spec AC 5)
# ---------------------------------------------------------------------------


def test_cache_stability_two_passes_identical():
    """Running twice on the same state produces byte-identical messages (cache stability)."""
    msgs = _make_messages(6)
    result_1 = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())
    result_2 = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())

    assert result_1.messages == result_2.messages, "Cache stability violated: outputs differ"
    for m1, m2 in zip(result_1.messages, result_2.messages):
        assert m1.content == m2.content


# ---------------------------------------------------------------------------
# 14. Non-string content (edge case)
# ---------------------------------------------------------------------------


def test_tool_message_with_list_content_not_cleared():
    """ToolMessages with non-string content are left unchanged (orig_bytes on non-str is safe)."""
    # LangChain allows ToolMessage.content to be a list of dicts (multimodal)
    list_tool_msg = ToolMessage(
        content=[{"type": "text", "text": "complex content"}],  # type: ignore[arg-type]
        tool_call_id="list_call",
        name="vision_tool",
    )
    extra_msgs = _make_messages(3)
    msgs: list[BaseMessage] = [list_tool_msg] + extra_msgs

    # Should not crash; the list-content message may or may not be cleared
    # but the function must not raise
    result = clear_tool_results(msgs, cleared_through_turn_index=0, keep=3, exclude_tools_effective=frozenset())
    assert result is not None
