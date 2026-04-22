"""Bedrock Converse prompt-cache strategy.

Bedrock's Converse API exposes prompt caching via a dedicated
``{"cachePoint": {"type": "default"}}`` block inserted *after* the block
you want cached — the native Anthropic ``cache_control`` inline field on a
text block is silently dropped by ``langchain-aws`` when it translates
messages into Converse format (verified against ``langchain-aws==1.4.0``,
``_lc_content_to_bedrock``). Using the Anthropic strategy verbatim on
Bedrock would therefore produce requests that *look* cache-enabled in the
LangChain layer but arrive uncached at the provider.

Marker placement matches the Anthropic strategy conceptually — one
breakpoint on the trailing system region, one on the tail message — but
the *mechanism* differs: we convert string content to a list shape and
append a ``cachePoint`` block rather than mutating a text block's
``cache_control`` field. The translator forwards the ``cachePoint`` block
unchanged because of its ``"type" not in block`` pass-through branch.

Usage extraction reuses :class:`AnthropicPromptCacheStrategy`'s logic
because Bedrock Converse reports cache tokens through the same
``usage_metadata.input_token_details`` shape (``cache_creation`` /
``cache_read`` keys) after LangChain normalisation.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage

from executor.prompt_cache.anthropic import AnthropicPromptCacheStrategy


_CACHE_POINT_BLOCK: dict[str, Any] = {"cachePoint": {"type": "default"}}


# Bedrock hosts many model families but only Anthropic Claude and Amazon
# Nova currently accept prompt caching. Sending ``cachePoint`` to any other
# family (GLM, Llama, Mistral, Cohere, AI21, Jamba) trips
# ``AccessDeniedException: ... did not allow prompt caching`` and dead-letters
# the task. Allowlist the two known-good markers; anything else is a no-op.
#
# Cross-region inference profiles prepend a region prefix to the model id
# (see https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html).
# Known prefixes as of 2026-04: ``us.``, ``us-gov.``, ``eu.``, ``apac.``,
# ``au.``, ``ca.``, ``jp.``, ``global.``. Strip the first region segment
# before the allowlist check. ``us-gov`` is listed before ``us`` so the
# longer match wins.
_BEDROCK_CACHE_CAPABLE_MARKERS: tuple[str, ...] = (
    "anthropic.",
    "amazon.nova-",
)
_BEDROCK_REGION_PREFIX_RE = re.compile(
    r"^(us-gov|us|eu|apac|au|ca|jp|global)\."
)


def _bedrock_supports_caching(model: str) -> bool:
    """Return True when *model* accepts Bedrock ``cachePoint`` markers.

    Accepts bare model ids (``anthropic.claude-3-5-sonnet-...``), regional
    inference profile ids (``us.anthropic.claude-3-5-sonnet-...``,
    ``jp.amazon.nova-lite-v1:0``, ``us-gov.anthropic.claude-3-haiku-...``),
    and full ARNs (``arn:aws:bedrock:us-east-1::foundation-model/anthropic.
    claude-...`` or ``...:inference-profile/us.anthropic.claude-...``). The
    ARN path falls back to a substring search so a future ARN layout
    change doesn't silently turn caching off for the whole fleet.
    """
    if not model:
        return False
    lower = model.strip().lower()
    # ARN / resource-path shape — match the family marker anywhere after a
    # ``/`` or ``.`` separator. Accepts both foundation-model and
    # inference-profile ARNs without prescribing the exact layout.
    if lower.startswith("arn:"):
        return any(
            f"/{marker}" in lower or f".{marker}" in lower
            for marker in _BEDROCK_CACHE_CAPABLE_MARKERS
        )
    normalized = _BEDROCK_REGION_PREFIX_RE.sub("", lower)
    return any(
        normalized.startswith(marker)
        for marker in _BEDROCK_CACHE_CAPABLE_MARKERS
    )


def _has_cache_point_tail(content: Any) -> bool:
    """Return True when *content* already ends with a cachePoint block."""
    if not isinstance(content, list) or not content:
        return False
    tail = content[-1]
    return (
        isinstance(tail, dict)
        and isinstance(tail.get("cachePoint"), dict)
        and tail["cachePoint"].get("type") is not None
    )


def _append_cache_point(content: Any) -> Any:
    """Return a new content list with a trailing cachePoint block.

    * ``str`` → converted to ``[{"type": "text", "text": content},
      cachePoint]``.
    * ``list`` → deep-copied, cachePoint appended at the tail. Empty
      lists and lists with no text-bearing block are left unchanged
      (a cachePoint with nothing preceding it is not meaningful).
    * Anything else → unchanged.
    """
    if isinstance(content, str):
        if not content:
            return content
        return [
            {"type": "text", "text": content},
            dict(_CACHE_POINT_BLOCK),
        ]
    if isinstance(content, list):
        if not content:
            return content
        if _has_cache_point_tail(content):
            return content
        # Only attach when there is at least one text-bearing block to
        # cache — a cachePoint after pure tool_use blocks is a no-op from
        # the Converse API's perspective and clutters the request.
        has_text = any(
            isinstance(b, dict)
            and (
                (b.get("type") == "text" and b.get("text"))
                or (b.get("type") is None and isinstance(b.get("text"), str) and b["text"])
            )
            for b in content
        )
        if not has_text:
            return content
        new_content = deepcopy(content)
        new_content.append(dict(_CACHE_POINT_BLOCK))
        return new_content
    return content


def _mark_message(msg: BaseMessage) -> BaseMessage:
    new_content = _append_cache_point(msg.content)
    if new_content is msg.content:
        return msg
    return msg.model_copy(update={"content": new_content})


class BedrockPromptCacheStrategy(AnthropicPromptCacheStrategy):
    """Bedrock-specific marker placement; Anthropic-shaped usage extraction."""

    provider = "bedrock"

    def supports_caching(self, model: str) -> bool:
        return _bedrock_supports_caching(model)

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

        tail_idx = len(out) - 1
        if tail_idx != last_system_idx:
            out[tail_idx] = _mark_message(out[tail_idx])

        return out
