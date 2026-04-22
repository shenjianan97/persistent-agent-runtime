"""Anthropic native prompt-cache strategy.

Marker placement rationale
--------------------------
Anthropic allows up to four ``cache_control`` breakpoints per request.  We
use at most two — enough for a high hit-rate in the multi-turn agent loop
without spending breakpoints we can't justify:

1. **Trailing SystemMessage.** The projection always leads with one or more
   ``SystemMessage``s (platform system + user system + optional compaction
   summary). Marking the last block of the last system message caches the
   entire system region as a single prefix. The summary message mutates
   only when Tier-3 fires, so this breakpoint stays valid across most turns.
2. **Last message overall.** Usually a ``HumanMessage`` or ``ToolMessage``.
   This is the sliding-window breakpoint: on turn ``N+1``, what was the
   tail on turn ``N`` becomes an interior prefix, and the request enjoys a
   cache hit for everything up to that point.

Content reshaping
-----------------
Anthropic requires cache markers to live on individual content blocks, so
if a message's ``content`` is a plain string we convert it to the list
shape ``[{"type": "text", "text": ..., "cache_control": {...}}]``. List
content is handled by mutating the last text-bearing block in place (on a
copy) so provider-shaped blocks carrying prompt caching keys / reasoning
state from earlier turns round-trip unchanged.

Usage extraction
----------------
LangChain's ``ChatAnthropic`` surfaces cache tokens in
``response.usage_metadata`` as ``input_token_details = {"cache_creation":
N, "cache_read": M}`` and in the raw ``response_metadata["usage"]`` as
``cache_creation_input_tokens`` / ``cache_read_input_tokens``. We read
both shapes so older and newer LangChain versions both work, and so the
native Anthropic SDK shape (used by the direct-SDK summariser path) is
covered.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage

from executor.prompt_cache.strategy import PromptCacheStrategy, TokenUsage


_CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}


def _apply_marker_to_content(content: Any) -> Any:
    """Return a new content value with a cache_control marker on the last
    text-bearing block.

    * ``str`` → wrap in a single text block with the marker.
    * ``list`` → copy, find the last block that carries text (``type: text``
      or a bare ``{"text": ...}`` dict), and add the marker in place.
      Non-text trailing blocks (e.g. ``tool_use``, ``thinking``) are passed
      over so we don't attach a marker to a block shape the provider doesn't
      accept it on.
    * Anything else → returned unchanged (defensive; the projection
      shouldn't produce non-str / non-list content in practice).
    """
    if isinstance(content, str):
        if not content:
            # Empty string — no meaningful prefix to cache; skip.
            return content
        return [
            {
                "type": "text",
                "text": content,
                "cache_control": dict(_CACHE_CONTROL_EPHEMERAL),
            }
        ]
    if isinstance(content, list):
        new_content = deepcopy(content)
        for idx in range(len(new_content) - 1, -1, -1):
            block = new_content[idx]
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            has_text = isinstance(block.get("text"), str) and block["text"]
            # Accept canonical text blocks and bare-dict {"text": "..."}
            # shapes. Skip tool_use / tool_result / thinking / image blocks —
            # cache_control on those shapes is not meaningful for our
            # placement and some provider SDKs reject it.
            if block_type in (None, "text") and has_text:
                block["cache_control"] = dict(_CACHE_CONTROL_EPHEMERAL)
                return new_content
        # No eligible block found — leave content unchanged.
        return new_content
    return content


def _mark_message(msg: BaseMessage) -> BaseMessage:
    """Return a copy of *msg* with a cache marker on the last text block."""
    new_content = _apply_marker_to_content(msg.content)
    if new_content is msg.content:
        return msg
    return msg.model_copy(update={"content": new_content})


class AnthropicPromptCacheStrategy(PromptCacheStrategy):
    provider = "anthropic"

    def supports_caching(self, model: str) -> bool:
        # Every current Claude model on the native Anthropic API accepts
        # cache_control (Claude 3, 3.5, 3.7, 4.x). We don't gate by model
        # here — if a future model drops support, the call still succeeds
        # (the field is silently ignored) and we'll notice via metrics.
        return True

    def apply_cache_markers(
        self, messages: list[BaseMessage]
    ) -> list[BaseMessage]:
        if not messages:
            return list(messages)

        out = list(messages)
        last_system_idx: int | None = None
        for idx in range(len(out) - 1, -1, -1):
            if isinstance(out[idx], SystemMessage):
                last_system_idx = idx
                break

        if last_system_idx is not None:
            out[last_system_idx] = _mark_message(out[last_system_idx])

        # Mark the tail message too — gives us the sliding-window breakpoint.
        # Skip the SystemMessage case (already marked) to avoid double-
        # marking when the projection is system-only (shouldn't happen in the
        # real agent loop, but be defensive).
        tail_idx = len(out) - 1
        if tail_idx != last_system_idx:
            out[tail_idx] = _mark_message(out[tail_idx])

        return out

    def extract_token_usage(self, response_metadata: dict) -> TokenUsage:
        # Anthropic usage surfaces in two shapes that DISAGREE on what
        # ``input_tokens`` includes:
        #   * Native SDK / raw response (``response_metadata["usage"]``):
        #     ``input_tokens`` excludes cached tokens — total prompt =
        #     input + cache_creation + cache_read.
        #   * LangChain-normalized (``usage_metadata``): langchain-anthropic
        #     rolls cache_read + cache_creation INTO ``input_tokens`` so
        #     total_tokens math works out. See
        #     ``langchain_anthropic.chat_models._create_usage_metadata`` —
        #     "Calculate total input tokens: Anthropic's input_tokens
        #     excludes cached tokens, so we manually add..."
        #
        # TokenUsage's contract is that ``input_tokens`` is the NON-CACHED
        # portion. Normalize the LangChain shape by subtracting cache
        # counters; the native shape needs no adjustment. Reading both
        # lets the summarizer (direct SDK) and the agent loop (LangChain)
        # share a single extractor.
        usage_metadata = response_metadata.get("usage_metadata") or {}
        usage_native = response_metadata.get("usage") or {}

        # LangChain standardised input_token_details in langchain-core 0.3+;
        # fall back to the native Anthropic keys for defence in depth.
        details = usage_metadata.get("input_token_details") or {}
        cache_creation = int(
            details.get("cache_creation")
            or usage_metadata.get("cache_creation_input_tokens")
            or usage_native.get("cache_creation_input_tokens")
            or 0
        )
        cache_read = int(
            details.get("cache_read")
            or usage_metadata.get("cache_read_input_tokens")
            or usage_native.get("cache_read_input_tokens")
            or 0
        )

        output_t = int(
            usage_metadata.get("output_tokens")
            or usage_native.get("output_tokens")
            or 0
        )

        # Prefer the native shape (unambiguous). Fall back to LangChain
        # shape with the inclusive-total adjustment.
        if usage_native.get("input_tokens") is not None:
            input_t = int(usage_native["input_tokens"] or 0)
        elif usage_metadata.get("input_tokens") is not None:
            raw = int(usage_metadata["input_tokens"] or 0)
            input_t = max(0, raw - cache_creation - cache_read)
        else:
            input_t = 0

        return TokenUsage(
            input_tokens=input_t,
            output_tokens=output_t,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        )
