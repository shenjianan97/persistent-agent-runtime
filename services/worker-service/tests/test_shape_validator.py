"""Self-tests for LLMConversationShapeValidator.

These pin down exactly what the validator catches (and what it permits)
so future regressions in the validator itself are visible.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from tests.shape_validator import (
    LLMConversationShapeValidator,
    ShapeViolation,
    assert_valid_shape,
)


def _ai_with_tools(call_id: str, name: str = "tool") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": name, "args": {}, "type": "tool_call"}],
    )


def _tool_result(call_id: str, name: str = "tool") -> ToolMessage:
    return ToolMessage(content="ok", tool_call_id=call_id, name=name)


class TestValidPasses:
    def test_empty_list_ok(self):
        assert_valid_shape([])

    def test_only_system_messages_ok(self):
        assert_valid_shape([SystemMessage(content="you are helpful")])

    def test_human_only_ok(self):
        assert_valid_shape([HumanMessage(content="hi")])

    def test_human_assistant_ok(self):
        assert_valid_shape([HumanMessage(content="hi"), AIMessage(content="hello")])

    def test_full_tool_use_cycle_ok(self):
        assert_valid_shape([
            HumanMessage(content="go"),
            _ai_with_tools("c1"),
            _tool_result("c1"),
            AIMessage(content="done"),
        ])

    def test_multiple_tool_calls_single_ai_ok(self):
        assert_valid_shape([
            HumanMessage(content="go"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "t", "args": {}, "type": "tool_call"},
                    {"id": "c2", "name": "t", "args": {}, "type": "tool_call"},
                ],
            ),
            _tool_result("c1"),
            _tool_result("c2"),
        ])

    def test_leading_system_then_human_ok(self):
        assert_valid_shape([
            SystemMessage(content="summary"),
            HumanMessage(content="go"),
        ])

    def test_tool_results_can_be_out_of_order(self):
        """Providers only require every id to be covered — order within
        a tool-result group isn't constrained."""
        assert_valid_shape([
            HumanMessage(content="go"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "t", "args": {}, "type": "tool_call"},
                    {"id": "c2", "name": "t", "args": {}, "type": "tool_call"},
                ],
            ),
            _tool_result("c2"),
            _tool_result("c1"),
        ])


class TestOrphanToolResult:
    def test_leading_tool_message_rejected(self):
        with pytest.raises(ShapeViolation, match="begins with a ToolMessage"):
            assert_valid_shape([_tool_result("c1")])

    def test_leading_after_system_still_rejected(self):
        with pytest.raises(ShapeViolation, match="begins with a ToolMessage"):
            assert_valid_shape([
                SystemMessage(content="summary"),
                _tool_result("c1"),
            ])

    def test_tool_message_with_unknown_id_rejected(self):
        with pytest.raises(ShapeViolation, match="no prior AIMessage"):
            assert_valid_shape([
                HumanMessage(content="go"),
                _ai_with_tools("c1"),
                _tool_result("c2"),  # wrong id
            ])


class TestPendingToolCalls:
    def test_ai_with_unmatched_call_rejected(self):
        with pytest.raises(ShapeViolation, match="pending tool_call"):
            assert_valid_shape([
                HumanMessage(content="go"),
                _ai_with_tools("c1"),
                # ToolMessage missing
                AIMessage(content="done"),
            ])

    def test_ai_with_partial_coverage_rejected(self):
        with pytest.raises(ShapeViolation, match="pending tool_call"):
            assert_valid_shape([
                HumanMessage(content="go"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "c1", "name": "t", "args": {}, "type": "tool_call"},
                        {"id": "c2", "name": "t", "args": {}, "type": "tool_call"},
                    ],
                ),
                _tool_result("c1"),
                # c2 missing
            ])


class TestReproductionOfProductionBug:
    def test_tier3_orphan_tool_tail_rejected(self):
        """The exact shape Tier 3 produced for task 3b8d422f before the fix:
        summary SystemMessage followed by an orphan ToolMessage."""
        messages = [
            SystemMessage(content="SUMMARY of earlier steps", additional_kwargs={"compaction": True}),
            _tool_result("tooluse_7SUeRPIQyQHnVwJx5kKzxP"),
            AIMessage(content="continuing..."),
        ]
        with pytest.raises(ShapeViolation, match="begins with a ToolMessage"):
            LLMConversationShapeValidator().validate(messages)
