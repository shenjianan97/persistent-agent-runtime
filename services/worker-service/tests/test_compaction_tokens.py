"""Unit tests for executor.compaction.tokens — token estimation.

Tests follow TDD: written before implementation.

Covers:
1. Heuristic fallback (provider="other") — char/3 based estimate
2. OpenAI path via tiktoken (if available, else skip)
3. Anthropic path (mocked to avoid real API calls)
4. Determinism: same messages always produce same count
5. _serialize_for_token_count — determinism across checkpoint round-trips
6. Empty messages list
7. Multi-content messages (block-list format)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from executor.compaction.tokens import estimate_tokens, _serialize_for_token_count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_call(tool_name: str, args: dict, call_id: str = "call_1") -> dict:
    return {"id": call_id, "name": tool_name, "args": args, "type": "tool_call"}


# ---------------------------------------------------------------------------
# 1. Serialize for token count
# ---------------------------------------------------------------------------


class TestSerializeForTokenCount:
    def test_empty_list_returns_string(self):
        result = _serialize_for_token_count([])
        assert isinstance(result, str)

    def test_human_message_included(self):
        msgs = [HumanMessage(content="hello world")]
        result = _serialize_for_token_count(msgs)
        assert "hello world" in result

    def test_system_message_included(self):
        msgs = [SystemMessage(content="You are helpful.")]
        result = _serialize_for_token_count(msgs)
        assert "You are helpful." in result

    def test_ai_message_content_included(self):
        msgs = [AIMessage(content="I will help you.")]
        result = _serialize_for_token_count(msgs)
        assert "I will help you." in result

    def test_tool_message_content_included(self):
        msgs = [ToolMessage(content="{'key': 'val'}", tool_call_id="call_1")]
        result = _serialize_for_token_count(msgs)
        assert "val" in result

    def test_tool_call_name_included(self):
        msgs = [AIMessage(
            content="",
            tool_calls=[_make_tool_call("my_tool", {"arg": "value"})],
        )]
        result = _serialize_for_token_count(msgs)
        assert "my_tool" in result

    def test_tool_call_args_serialized_with_sorted_keys(self):
        """Sorted keys are required for determinism."""
        args = {"z_key": "last", "a_key": "first"}
        msgs = [AIMessage(
            content="",
            tool_calls=[_make_tool_call("tool", args)],
        )]
        result = _serialize_for_token_count(msgs)
        # json.dumps(sort_keys=True) should produce a_key before z_key
        a_pos = result.index("a_key")
        z_pos = result.index("z_key")
        assert a_pos < z_pos

    def test_deterministic_on_repeated_calls(self):
        """Same messages always produce byte-identical serialization."""
        msgs = [
            SystemMessage(content="System prompt"),
            HumanMessage(content="User input"),
            AIMessage(content="Response", tool_calls=[
                _make_tool_call("tool_x", {"b": 2, "a": 1}),
            ]),
            ToolMessage(content="tool result", tool_call_id="call_1"),
        ]
        first = _serialize_for_token_count(msgs)
        second = _serialize_for_token_count(msgs)
        assert first == second

    def test_response_metadata_excluded(self):
        """response_metadata, additional_kwargs, usage_metadata must NOT appear."""
        msg = AIMessage(
            content="text",
            response_metadata={"model": "claude-3", "usage": {"input_tokens": 100}},
            additional_kwargs={"secret": "internal"},
        )
        result = _serialize_for_token_count([msg])
        # These internal fields must not leak into the serialized form
        assert "response_metadata" not in result
        assert "additional_kwargs" not in result
        assert "usage" not in result

    def test_block_list_content_flattened(self):
        """AIMessage with block-list content: only text blocks included."""
        msg = AIMessage(content=[
            {"type": "text", "text": "Hello block"},
            {"type": "tool_use", "id": "x", "name": "t", "input": {}},
        ])
        result = _serialize_for_token_count([msg])
        assert "Hello block" in result


# ---------------------------------------------------------------------------
# 2. estimate_tokens — heuristic fallback
# ---------------------------------------------------------------------------


class TestEstimateTokensHeuristic:
    def test_heuristic_fallback_for_unknown_provider(self):
        """Unknown providers fall back to char/3 heuristic."""
        from executor.compaction.tokens import _serialize_for_token_count
        msgs = [HumanMessage(content="a" * 300)]
        serialized = _serialize_for_token_count(msgs)
        expected = len(serialized.encode("utf-8")) // 3
        count = estimate_tokens(msgs, provider="unknown_provider")
        assert count == expected

    def test_heuristic_empty_messages(self):
        count = estimate_tokens([], provider="other")
        assert count == 0

    def test_heuristic_provider_gemini(self):
        """Gemini explicitly falls back to heuristic."""
        msgs = [HumanMessage(content="abc")]
        count = estimate_tokens(msgs, provider="google")
        # len("abc") = 3 bytes → 3//3 = 1
        assert count >= 0

    def test_heuristic_returns_int(self):
        msgs = [HumanMessage(content="test")]
        count = estimate_tokens(msgs, provider="byot")
        assert isinstance(count, int)


# ---------------------------------------------------------------------------
# 3. estimate_tokens — OpenAI (tiktoken)
# ---------------------------------------------------------------------------


class TestEstimateTokensOpenAI:
    def test_openai_returns_positive_int(self):
        """With tiktoken available, OpenAI path must return a positive int."""
        try:
            import tiktoken  # noqa: F401
        except ImportError:
            pytest.skip("tiktoken not installed")
        msgs = [HumanMessage(content="Hello, how are you?")]
        count = estimate_tokens(msgs, provider="openai")
        assert isinstance(count, int)
        assert count > 0

    def test_openai_more_tokens_for_longer_message(self):
        try:
            import tiktoken  # noqa: F401
        except ImportError:
            pytest.skip("tiktoken not installed")
        short = [HumanMessage(content="hi")]
        long = [HumanMessage(content="hi " * 100)]
        short_count = estimate_tokens(short, provider="openai")
        long_count = estimate_tokens(long, provider="openai")
        assert long_count > short_count

    def test_openai_deterministic(self):
        try:
            import tiktoken  # noqa: F401
        except ImportError:
            pytest.skip("tiktoken not installed")
        msgs = [HumanMessage(content="Same message")]
        c1 = estimate_tokens(msgs, provider="openai")
        c2 = estimate_tokens(msgs, provider="openai")
        assert c1 == c2


# ---------------------------------------------------------------------------
# 4. estimate_tokens — Anthropic (mocked)
# ---------------------------------------------------------------------------


class TestEstimateTokensAnthropic:
    def test_anthropic_uses_count_tokens(self):
        """Anthropic path calls anthropic.Anthropic().count_tokens(serialized)."""
        mock_client = MagicMock()
        mock_client.count_tokens.return_value = 42

        with patch("executor.compaction.tokens._get_anthropic_client", return_value=mock_client):
            msgs = [HumanMessage(content="test")]
            count = estimate_tokens(msgs, provider="anthropic")

        assert count == 42
        mock_client.count_tokens.assert_called_once()

    def test_anthropic_falls_back_to_heuristic_on_import_error(self):
        """If anthropic package import fails, fall back to heuristic."""
        import sys
        original = sys.modules.get("anthropic")
        try:
            sys.modules["anthropic"] = None  # type: ignore
            msgs = [HumanMessage(content="hello")]
            count = estimate_tokens(msgs, provider="anthropic")
            # Should not raise; returns heuristic estimate
            assert isinstance(count, int)
            assert count >= 0
        finally:
            if original is not None:
                sys.modules["anthropic"] = original
            elif "anthropic" in sys.modules:
                del sys.modules["anthropic"]

    def test_anthropic_falls_back_on_exception(self):
        """If count_tokens raises, fall back to heuristic without crashing."""
        mock_client = MagicMock()
        mock_client.count_tokens.side_effect = RuntimeError("API error")

        with patch("executor.compaction.tokens._get_anthropic_client", return_value=mock_client):
            msgs = [HumanMessage(content="hello")]
            count = estimate_tokens(msgs, provider="anthropic")

        assert isinstance(count, int)
        assert count >= 0


# ---------------------------------------------------------------------------
# 5. Determinism across checkpoint round-trip
# ---------------------------------------------------------------------------


class TestDeterminismAcrossRoundTrip:
    def test_pre_post_checkpoint_same_estimate(self):
        """estimate_tokens must produce the same count before and after simulating
        a checkpoint round-trip (serialization/deserialization of messages).

        This test simulates checkpoint round-trip by reconstructing messages
        from their text content only (as the serializer should see them).
        """
        msgs = [
            SystemMessage(content="System"),
            HumanMessage(content="User message with details"),
            AIMessage(
                content="AI response",
                tool_calls=[_make_tool_call("my_tool", {"content": "large arg", "b": 2})],
                # These fields can vary between pre/post checkpoint
                response_metadata={"model_id": "claude-3"},
                id="msg_abc123",
            ),
            ToolMessage(content="result data", tool_call_id="call_1", name="my_tool"),
        ]

        # Simulate "pre-checkpoint" estimate
        count_before = estimate_tokens(msgs, provider="other")

        # Simulate "post-checkpoint" — reconstruct messages from essential fields only
        # (as a real checkpoint round-trip would do)
        msgs_restored = [
            SystemMessage(content="System"),
            HumanMessage(content="User message with details"),
            AIMessage(
                content="AI response",
                tool_calls=[_make_tool_call("my_tool", {"content": "large arg", "b": 2})],
                # Different id / response_metadata — common after checkpoint load
                response_metadata={},
                id="different_id",
            ),
            ToolMessage(content="result data", tool_call_id="call_1", name="my_tool"),
        ]

        count_after = estimate_tokens(msgs_restored, provider="other")

        # The allow-list serializer must produce the same count
        assert count_before == count_after, (
            f"Token count changed across checkpoint round-trip: "
            f"{count_before} != {count_after}"
        )
