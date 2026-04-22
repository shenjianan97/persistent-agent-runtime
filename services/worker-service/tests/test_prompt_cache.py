"""Unit tests for the provider-agnostic prompt-cache strategy.

Covers marker placement, content shape preservation, and token-usage
extraction for each registered provider. These tests are the contract
every new strategy must pass — if you add a provider, add tests here
with the same structure.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from executor.prompt_cache import TokenUsage, get_strategy
from executor.prompt_cache.anthropic import AnthropicPromptCacheStrategy
from executor.prompt_cache.bedrock import BedrockPromptCacheStrategy
from executor.prompt_cache.noop import NoopPromptCacheStrategy
from executor.prompt_cache.openai import OpenAIPromptCacheStrategy


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_anthropic():
    assert isinstance(get_strategy("anthropic"), AnthropicPromptCacheStrategy)


def test_registry_bedrock():
    assert isinstance(get_strategy("bedrock"), BedrockPromptCacheStrategy)


def test_registry_openai():
    assert isinstance(get_strategy("openai"), OpenAIPromptCacheStrategy)


def test_registry_unknown_provider_falls_back_to_noop():
    strat = get_strategy("some-future-provider")
    assert isinstance(strat, NoopPromptCacheStrategy)


# ---------------------------------------------------------------------------
# Anthropic: marker placement
# ---------------------------------------------------------------------------


def _find_cache_marked_blocks(content):
    """Return the list of blocks whose ``cache_control`` field is set."""
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("cache_control")]


def test_anthropic_marks_string_system_message():
    strategy = AnthropicPromptCacheStrategy()
    msgs = [SystemMessage(content="you are a helpful agent")]
    out = strategy.apply_cache_markers(msgs)

    assert len(out) == 1
    marked = _find_cache_marked_blocks(out[0].content)
    assert len(marked) == 1
    assert marked[0]["cache_control"] == {"type": "ephemeral"}
    assert marked[0]["text"] == "you are a helpful agent"


def test_anthropic_marks_last_system_and_last_message():
    strategy = AnthropicPromptCacheStrategy()
    msgs = [
        SystemMessage(content="platform system"),
        SystemMessage(content="user system"),
        HumanMessage(content="hello"),
        AIMessage(content="hi there"),
        HumanMessage(content="do the thing"),
    ]
    out = strategy.apply_cache_markers(msgs)

    # Last SystemMessage (idx 1) marked.
    assert _find_cache_marked_blocks(out[1].content)
    # Intermediate messages not marked.
    assert not _find_cache_marked_blocks(out[2].content)
    assert not _find_cache_marked_blocks(out[3].content)
    # Last message marked (sliding-window breakpoint).
    assert _find_cache_marked_blocks(out[4].content)
    # First SystemMessage (not last) not marked.
    assert not _find_cache_marked_blocks(out[0].content)


def test_anthropic_preserves_list_content_blocks():
    """List-shape content with multiple blocks must keep all blocks; only
    the last text-bearing block gains a cache marker."""
    strategy = AnthropicPromptCacheStrategy()
    original_blocks = [
        {"type": "text", "text": "first paragraph"},
        {"type": "text", "text": "second paragraph"},
    ]
    msgs = [SystemMessage(content=list(original_blocks))]
    out = strategy.apply_cache_markers(msgs)

    new_content = out[0].content
    assert isinstance(new_content, list)
    assert len(new_content) == 2
    assert "cache_control" not in new_content[0]
    assert new_content[1]["cache_control"] == {"type": "ephemeral"}

    # Input list untouched.
    assert "cache_control" not in original_blocks[0]
    assert "cache_control" not in original_blocks[1]


def test_anthropic_skips_tool_use_trailing_block():
    """cache_control on ``tool_use`` blocks is not a supported placement;
    the strategy must walk back to the nearest text block."""
    strategy = AnthropicPromptCacheStrategy()
    msgs = [
        AIMessage(
            content=[
                {"type": "text", "text": "calling a tool"},
                {
                    "type": "tool_use",
                    "id": "tool_0",
                    "name": "search",
                    "input": {},
                },
            ]
        )
    ]
    out = strategy.apply_cache_markers(msgs)

    new_content = out[0].content
    # Text block gets the marker.
    assert new_content[0].get("cache_control") == {"type": "ephemeral"}
    # Tool-use block untouched.
    assert "cache_control" not in new_content[1]


def test_anthropic_no_text_block_noop():
    """Content with no eligible text block → returned unchanged, not crashed."""
    strategy = AnthropicPromptCacheStrategy()
    msgs = [
        AIMessage(
            content=[
                {"type": "tool_use", "id": "t0", "name": "s", "input": {}},
            ]
        )
    ]
    out = strategy.apply_cache_markers(msgs)

    assert len(out[0].content) == 1
    assert "cache_control" not in out[0].content[0]


def test_anthropic_empty_string_content_not_marked():
    """An empty-string content has no meaningful prefix to cache; skip it."""
    strategy = AnthropicPromptCacheStrategy()
    msgs = [HumanMessage(content="")]
    out = strategy.apply_cache_markers(msgs)

    assert out[0].content == ""


def test_anthropic_returns_new_list_instance():
    """Pure function: caller's list must not be mutated."""
    strategy = AnthropicPromptCacheStrategy()
    msgs = [SystemMessage(content="s"), HumanMessage(content="h")]
    out = strategy.apply_cache_markers(msgs)

    assert out is not msgs
    assert msgs[0].content == "s"


def test_anthropic_empty_messages():
    strategy = AnthropicPromptCacheStrategy()
    assert strategy.apply_cache_markers([]) == []


# ---------------------------------------------------------------------------
# Anthropic: token extraction
# ---------------------------------------------------------------------------


def test_anthropic_extracts_from_raw_usage():
    strategy = AnthropicPromptCacheStrategy()
    metadata = {
        "usage": {
            "input_tokens": 50,
            "output_tokens": 25,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 500,
        }
    }
    usage = strategy.extract_token_usage(metadata)

    assert usage == TokenUsage(
        input_tokens=50,
        output_tokens=25,
        cache_creation_input_tokens=100,
        cache_read_input_tokens=500,
    )


def test_anthropic_langchain_shape_subtracts_cache_from_input_tokens() -> None:
    """``usage_metadata.input_tokens`` from langchain-anthropic INCLUDES
    cache_read + cache_creation (see langchain-anthropic's
    _create_usage_metadata: "we manually add cache_read and
    cache_creation to get the true total"). Our extractor must subtract
    them to preserve the TokenUsage invariant that ``input_tokens`` is
    the non-cached portion. Double-counting would bill Anthropic cache
    tokens both at input rate AND at their dedicated rate.
    """
    strategy = AnthropicPromptCacheStrategy()
    usage = strategy.extract_token_usage(
        {
            # No native ``usage`` shape — force the fallback path.
            "usage_metadata": {
                "input_tokens": 10_000,  # base 1000 + cache_read 8000 + cache_creation 1000
                "output_tokens": 150,
                "input_token_details": {
                    "cache_read": 8_000,
                    "cache_creation": 1_000,
                },
            },
        }
    )
    assert usage.input_tokens == 1_000
    assert usage.cache_read_input_tokens == 8_000
    assert usage.cache_creation_input_tokens == 1_000
    # Invariant: input + cache_read + cache_creation = total prompt
    assert usage.total_prompt_tokens == 10_000


def test_anthropic_native_shape_keeps_input_tokens_unchanged() -> None:
    """Native Anthropic SDK shape (used by the direct-SDK summarizer
    path): ``usage.input_tokens`` is already the non-cached portion.
    No subtraction needed."""
    strategy = AnthropicPromptCacheStrategy()
    usage = strategy.extract_token_usage(
        {
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 150,
                "cache_read_input_tokens": 8_000,
                "cache_creation_input_tokens": 1_000,
            },
        }
    )
    assert usage.input_tokens == 1_000
    assert usage.cache_read_input_tokens == 8_000
    assert usage.cache_creation_input_tokens == 1_000
    assert usage.total_prompt_tokens == 10_000


def test_anthropic_prefers_native_over_metadata_when_both_present() -> None:
    """When both shapes are present (LangChain AIMessage with raw
    response_metadata carried through), prefer the unambiguous native
    shape. Defends against a future LangChain version silently changing
    the ``usage_metadata.input_tokens`` convention."""
    strategy = AnthropicPromptCacheStrategy()
    usage = strategy.extract_token_usage(
        {
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 150,
                "cache_read_input_tokens": 8_000,
            },
            "usage_metadata": {
                "input_tokens": 9_000,  # inclusive — would double-count if preferred
                "output_tokens": 150,
                "input_token_details": {"cache_read": 8_000},
            },
        }
    )
    assert usage.input_tokens == 1_000  # native wins


def test_anthropic_extracts_from_usage_metadata_details():
    """LangChain-normalised shape: input_token_details carries the cache
    counters."""
    strategy = AnthropicPromptCacheStrategy()
    metadata = {
        "usage_metadata": {
            "input_tokens": 10,
            "output_tokens": 5,
            "input_token_details": {"cache_creation": 20, "cache_read": 80},
        }
    }
    usage = strategy.extract_token_usage(metadata)

    assert usage.cache_creation_input_tokens == 20
    assert usage.cache_read_input_tokens == 80


def test_anthropic_extracts_empty():
    strategy = AnthropicPromptCacheStrategy()
    usage = strategy.extract_token_usage({})
    assert usage == TokenUsage(input_tokens=0, output_tokens=0)


# ---------------------------------------------------------------------------
# OpenAI: marker is a no-op; cached_tokens are extracted
# ---------------------------------------------------------------------------


def test_openai_marker_is_noop():
    strategy = OpenAIPromptCacheStrategy()
    msgs = [SystemMessage(content="s"), HumanMessage(content="h")]
    out = strategy.apply_cache_markers(msgs)

    # Content unchanged.
    assert out[0].content == "s"
    assert out[1].content == "h"


def test_openai_extracts_cached_tokens():
    strategy = OpenAIPromptCacheStrategy()
    metadata = {
        "token_usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
    }
    usage = strategy.extract_token_usage(metadata)

    # Normalised: input_tokens = prompt_tokens - cached_tokens
    assert usage.input_tokens == 200
    assert usage.output_tokens == 100
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 800


def test_openai_extracts_without_cache_details():
    strategy = OpenAIPromptCacheStrategy()
    metadata = {
        "token_usage": {"prompt_tokens": 100, "completion_tokens": 50}
    }
    usage = strategy.extract_token_usage(metadata)

    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_read_input_tokens == 0


# ---------------------------------------------------------------------------
# Bedrock: cachePoint block placement (different mechanism than Anthropic)
# ---------------------------------------------------------------------------


def _has_cache_point_tail(content):
    if not isinstance(content, list) or not content:
        return False
    tail = content[-1]
    return (
        isinstance(tail, dict)
        and isinstance(tail.get("cachePoint"), dict)
        and tail["cachePoint"].get("type") == "default"
    )


def test_bedrock_marks_last_system_and_last_message_with_cache_point_block():
    """Verified against ``langchain-aws==1.4.0``: Bedrock Converse needs a
    trailing ``{"cachePoint": {"type": "default"}}`` block, not an inline
    ``cache_control`` field (which the translator drops)."""
    strategy = BedrockPromptCacheStrategy()
    msgs = [
        SystemMessage(content="system"),
        HumanMessage(content="prompt"),
    ]
    out = strategy.apply_cache_markers(msgs)

    assert _has_cache_point_tail(out[0].content)
    assert _has_cache_point_tail(out[1].content)
    # No inline cache_control fields should leak through — those do nothing
    # on Bedrock and imply the wrong provider shape.
    assert not _find_cache_marked_blocks(out[0].content)
    assert not _find_cache_marked_blocks(out[1].content)


def test_bedrock_string_content_is_wrapped_in_list_shape():
    strategy = BedrockPromptCacheStrategy()
    msgs = [SystemMessage(content="you are helpful")]
    out = strategy.apply_cache_markers(msgs)

    new_content = out[0].content
    assert isinstance(new_content, list)
    assert new_content[0] == {"type": "text", "text": "you are helpful"}
    assert new_content[-1] == {"cachePoint": {"type": "default"}}


def test_bedrock_empty_string_not_marked():
    strategy = BedrockPromptCacheStrategy()
    msgs = [HumanMessage(content="")]
    out = strategy.apply_cache_markers(msgs)

    assert out[0].content == ""


def test_bedrock_does_not_double_mark():
    """Running the strategy twice must not produce two trailing cachePoint
    blocks — the per-turn replay path calls it every LLM invocation."""
    strategy = BedrockPromptCacheStrategy()
    msgs = [HumanMessage(content="hi")]
    once = strategy.apply_cache_markers(msgs)
    twice = strategy.apply_cache_markers(once)

    assert len(twice[0].content) == len(once[0].content)


def test_bedrock_extracts_cache_tokens():
    strategy = BedrockPromptCacheStrategy()
    metadata = {
        "usage_metadata": {
            "input_tokens": 10,
            "output_tokens": 5,
            "input_token_details": {"cache_creation": 0, "cache_read": 99},
        }
    }
    usage = strategy.extract_token_usage(metadata)

    assert usage.cache_read_input_tokens == 99


# ---------------------------------------------------------------------------
# Noop: both methods are pure passthrough
# ---------------------------------------------------------------------------


def test_noop_passthrough():
    strategy = NoopPromptCacheStrategy()
    msgs = [HumanMessage(content="hi"), ToolMessage(content="result", tool_call_id="t0")]
    out = strategy.apply_cache_markers(msgs)

    assert out[0].content == "hi"
    assert out[1].content == "result"


def test_noop_extracts_basic_usage():
    strategy = NoopPromptCacheStrategy()
    usage = strategy.extract_token_usage(
        {"usage": {"input_tokens": 10, "output_tokens": 5}}
    )
    assert usage == TokenUsage(input_tokens=10, output_tokens=5)


# ---------------------------------------------------------------------------
# WORKER_PROMPT_CACHE_DISABLED kill switch
# ---------------------------------------------------------------------------
#
# The env var is parsed by ``_prompt_cache_markers_disabled_by_env`` in
# ``executor.graph`` and frozen into the module-level constant
# ``_PROMPT_CACHE_MARKERS_ENABLED`` at import time.  These tests exercise the
# parser directly — testing the module constant would require a module reload
# dance that isn't worth the complexity for such a simple lever.


import os

from executor.graph import _prompt_cache_markers_disabled_by_env


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("  1  ", True),  # whitespace tolerated
    ],
)
def test_kill_switch_recognizes_truthy_values(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("WORKER_PROMPT_CACHE_DISABLED", value)
    assert _prompt_cache_markers_disabled_by_env() is expected


@pytest.mark.parametrize(
    "value",
    ["", "0", "false", "False", "no", "off", "unexpected", "  "],
)
def test_kill_switch_ignores_falsy_or_unknown_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The env var is a disable flag; only explicit truthy values disable.
    Ambiguous input stays enabled (safer default — caching only helps)."""
    monkeypatch.setenv("WORKER_PROMPT_CACHE_DISABLED", value)
    assert _prompt_cache_markers_disabled_by_env() is False


def test_kill_switch_defaults_to_enabled_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WORKER_PROMPT_CACHE_DISABLED", raising=False)
    assert _prompt_cache_markers_disabled_by_env() is False


# ---------------------------------------------------------------------------
# Model-level support gate
# ---------------------------------------------------------------------------
#
# Bedrock hosts many families; only Anthropic Claude and Amazon Nova accept
# ``cachePoint`` blocks. Sending markers to anything else (GLM, Llama,
# Mistral, Cohere) dead-letters the task with AccessDeniedException. The
# ``supports_caching(model)`` hook lets the graph skip marker injection for
# incompatible models without losing token-usage extraction.


@pytest.mark.parametrize(
    "model_id",
    [
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
        "anthropic.claude-3-7-sonnet-20250219-v1:0",
        "anthropic.claude-opus-4-v1:0",
        "anthropic.claude-haiku-4-5-v1:0",
        "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "eu.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "apac.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "global.anthropic.claude-opus-4-v1:0",
        "amazon.nova-lite-v1:0",
        "amazon.nova-pro-v1:0",
        "amazon.nova-micro-v1:0",
        "us.amazon.nova-lite-v1:0",
        # Regional inference profiles beyond the initial us/eu/apac/global set.
        "us-gov.anthropic.claude-3-haiku-v1:0",
        "au.anthropic.claude-sonnet-4-5-v1:0",
        "ca.amazon.nova-lite-v1:0",
        "jp.anthropic.claude-haiku-4-5-v1:0",
        # Full ARNs — foundation-model and inference-profile layouts.
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0",
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "arn:aws:bedrock:ap-northeast-1::foundation-model/amazon.nova-lite-v1:0",
    ],
)
def test_bedrock_supports_caching_for_known_families(model_id: str) -> None:
    assert BedrockPromptCacheStrategy().supports_caching(model_id) is True


@pytest.mark.parametrize(
    "model_id",
    [
        "zai.glm-5",
        "meta.llama3-70b-instruct-v1:0",
        "mistral.mistral-large-2407-v1:0",
        "cohere.command-r-plus-v1:0",
        "ai21.jamba-1-5-large-v1:0",
        "amazon.titan-text-express-v1",  # Titan (not Nova) — no caching
        "us.meta.llama3-70b-instruct-v1:0",
        "",
    ],
)
def test_bedrock_supports_caching_false_for_unsupported(model_id: str) -> None:
    assert BedrockPromptCacheStrategy().supports_caching(model_id) is False


def test_anthropic_and_openai_always_support_caching() -> None:
    assert AnthropicPromptCacheStrategy().supports_caching("claude-opus-4-7") is True
    assert AnthropicPromptCacheStrategy().supports_caching("") is True
    assert OpenAIPromptCacheStrategy().supports_caching("gpt-4o-mini") is True
    assert OpenAIPromptCacheStrategy().supports_caching("") is True


def test_noop_never_supports_caching() -> None:
    assert NoopPromptCacheStrategy().supports_caching("anything") is False


def test_kill_switch_only_suppresses_injection_not_extraction() -> None:
    """Documented invariant: the kill switch suppresses marker INJECTION
    only. Token-usage extraction must continue so OpenAI's automatic
    caching still reports correctly. This test is a contract reminder —
    it checks the registry still hands out real strategies regardless of
    env state, because extraction lives on the strategy.
    """
    for provider in ("anthropic", "bedrock", "openai", "some-unknown-future-provider"):
        strategy = get_strategy(provider)
        # Strategy resolution is provider-keyed; the env var is checked at
        # the agent_node call site, not inside strategies themselves.
        assert hasattr(strategy, "extract_token_usage")
        assert hasattr(strategy, "apply_cache_markers")
