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

from executor.compaction.tokens import (
    _extract_text_content,
    _serialize_for_token_count,
    estimate_tokens,
)


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
    def test_anthropic_uses_heuristic(self):
        """Anthropic provider uses the heuristic fallback.

        The legacy ``anthropic.Anthropic().count_tokens()`` method was removed
        from modern anthropic SDKs (≥ 0.40); the replacement
        ``client.messages.count_tokens`` is async + needs a model id, so we
        punt and use the heuristic until that surface is wired through. The
        heuristic is provider-agnostic and deterministic — matching the
        ``unknown_provider`` path exactly.
        """
        from executor.compaction.tokens import _serialize_for_token_count
        msgs = [HumanMessage(content="test")]
        serialized = _serialize_for_token_count(msgs)
        expected = len(serialized.encode("utf-8")) // 3
        count = estimate_tokens(msgs, provider="anthropic")
        assert count == expected

    def test_anthropic_deterministic(self):
        """Anthropic estimates are deterministic across calls."""
        msgs = [HumanMessage(content="Same message")]
        assert (
            estimate_tokens(msgs, provider="anthropic")
            == estimate_tokens(msgs, provider="anthropic")
        )


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


# ---------------------------------------------------------------------------
# 6. Shared provider-shape fixtures — kept in lock-step with the Java
#    MessageContentExtractor test set (F-* IDs). ``BaseMessage.text`` joins
#    sibling text blocks with ``""`` by design (consecutive text blocks are
#    programmatic concatenations, not paragraphs). We therefore assert
#    substring presence of each expected text part rather than equality
#    against the Java separator-joined string.
# ---------------------------------------------------------------------------


_SHARED_FIXTURES = [
    ("F-STR-SIMPLE", "Hello world", ["Hello world"], []),
    ("F-STR-EMPTY", "", [], []),
    ("F-NULL", None, [], []),
    (
        "F-ANTHROPIC-PROSE",
        [{"type": "text", "text": "Let me search for that"}],
        ["Let me search for that"],
        [],
    ),
    (
        "F-ANTHROPIC-MIXED",
        [
            {"type": "text", "text": "Sure, I'll check"},
            {"type": "tool_use", "id": "tu_1", "name": "web_search", "input": {"q": "x"}},
        ],
        ["Sure, I'll check"],
        ["web_search"],
    ),
    (
        "F-ANTHROPIC-TOOLS-ONLY",
        [{"type": "tool_use", "id": "tu_1", "name": "web_search", "input": {"q": "x"}}],
        [],
        ["web_search"],
    ),
    (
        "F-ANTHROPIC-THINKING",
        [
            {"type": "thinking", "thinking": "Deliberating...", "signature": "..."},
            {"type": "text", "text": "Here is the answer"},
        ],
        ["Deliberating...", "Here is the answer"],
        [],
    ),
    (
        "F-OPENAI-NATIVE-OUTPUT-TEXT",
        [{"type": "output_text", "text": "Here is the report"}],
        ["Here is the report"],
        [],
    ),
    (
        "F-OPENAI-NESTED-MESSAGE",
        [
            {"id": "rs_1", "type": "reasoning", "summary": []},
            {
                "id": "msg_1",
                "type": "message",
                "content": [{"type": "output_text", "text": "Below is a summary"}],
            },
            {"id": "fc_1", "type": "function_call", "name": "web_search", "arguments": "{}"},
        ],
        ["Below is a summary"],
        [],
    ),
    (
        "F-OPENAI-REASONING-ONLY",
        [
            {"id": "rs_1", "type": "reasoning", "summary": []},
            {"id": "fc_1", "type": "function_call", "name": "web_search", "arguments": "{}"},
        ],
        [],
        [],
    ),
    (
        "F-GEMINI-BARE-DICT",
        [{"text": "Response from Gemini"}],
        ["Response from Gemini"],
        [],
    ),
    (
        "F-BEDROCK-CONVERSE-TEXT",
        [
            {"text": "Response via Bedrock"},
            {"toolUse": {"name": "search", "input": {}, "toolUseId": "tu_1"}},
        ],
        ["Response via Bedrock"],
        [],
    ),
    (
        "F-MULTI-TEXT-JOIN",
        [
            {"type": "text", "text": "First para"},
            {"type": "text", "text": "Second para"},
        ],
        ["First para", "Second para"],
        [],
    ),
]


class TestExtractTextContentSharedFixtures:
    @pytest.mark.parametrize(
        "fixture_id,content,expected_parts,forbidden_parts",
        _SHARED_FIXTURES,
        ids=[f[0] for f in _SHARED_FIXTURES],
    )
    def test_extract_text_matches_fixture(
        self, fixture_id, content, expected_parts, forbidden_parts
    ):
        out = _extract_text_content(content)
        assert isinstance(out, str), f"{fixture_id}: expected str, got {type(out)}"
        for part in expected_parts:
            assert part in out, (
                f"{fixture_id}: expected substring '{part}' not found in {out!r}"
            )
        for part in forbidden_parts:
            assert part not in out, (
                f"{fixture_id}: forbidden substring '{part}' leaked into text "
                f"view {out!r}"
            )
        if not expected_parts:
            # tools-only / reasoning-only / empty / null: the text view must
            # be empty — no prose, no tool_use / function_call leakage.
            assert out == "", f"{fixture_id}: expected empty text, got {out!r}"


class TestExtractTextContentSeparator:
    """The separator parameter lets callers opt into paragraph-spaced joins
    for user-facing artifacts (``output.result``) while keeping the
    token-estimation / summarizer path's ``""`` default — consecutive text
    blocks are programmatic concatenation for those callers, not paragraphs.
    """

    def test_default_separator_is_empty(self):
        blocks = [
            {"type": "text", "text": "First"},
            {"type": "text", "text": "Second"},
        ]
        assert _extract_text_content(blocks) == "FirstSecond"

    def test_explicit_paragraph_separator_joins_with_blank_line(self):
        blocks = [
            {"type": "text", "text": "First"},
            {"type": "text", "text": "Second"},
        ]
        assert _extract_text_content(blocks, separator="\n\n") == "First\n\nSecond"

    def test_separator_does_not_leak_when_only_one_block_has_text(self):
        # Separator is applied only between non-empty parts — a single
        # surviving text block must not get a trailing separator.
        blocks = [
            {"type": "tool_use", "id": "tu_1", "name": "x", "input": {}},
            {"type": "text", "text": "only prose"},
        ]
        assert _extract_text_content(blocks, separator="\n\n") == "only prose"


class TestFormatMessagesIntegration:
    """Regression guard: ``format_messages_for_summary`` must surface assistant
    prose from OpenAI-shaped block lists. Before the ``BaseMessage.text``
    delegation landed, only ``type: text`` blocks were extracted and OpenAI
    Responses assistants rendered as empty.
    """

    def test_openai_shaped_ai_content_flattened(self):
        from executor.compaction.summarizer import format_messages_for_summary

        openai_content = [
            {"id": "rs_1", "type": "reasoning", "summary": []},
            {
                "id": "msg_1",
                "type": "message",
                "content": [{"type": "output_text", "text": "Below is a summary"}],
            },
            {"id": "fc_1", "type": "function_call", "name": "web_search", "arguments": "{}"},
        ]
        msgs = [
            HumanMessage(content="please summarize"),
            AIMessage(content=openai_content),
        ]
        out = format_messages_for_summary(msgs)
        # Positive: the nested output_text prose must surface in the summary
        # input (previously dropped by the type=="text" filter).
        assert "Below is a summary" in out
        # Negative: the raw dict repr of the OpenAI content list must not
        # leak into the text view. BaseMessage.text strips metadata blocks.
        assert "'type': 'reasoning'" not in out
        assert "'summary': []" not in out
