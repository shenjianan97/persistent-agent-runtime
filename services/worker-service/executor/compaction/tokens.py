"""Token estimation for Track 7 compaction pipeline.

Entry point: :func:`estimate_tokens`.

Uses the real tokenizer when available (tiktoken for OpenAI; Anthropic SDK
``count_tokens`` for Anthropic) and falls back to a ``len(serialized) // 3``
heuristic for Gemini, BYOT, and any unknown provider.

Imports are lazy so non-provider agents (e.g., Gemini-only) do not pay the
startup cost of importing packages they do not use.

Serialization contract (KV-cache stability requirement)
-------------------------------------------------------
:func:`_serialize_for_token_count` uses an explicit allow-list of message
fields:

  type | content | tool_calls[].name | tool_calls[].args (sorted keys)
  tool_call_id (ToolMessage)

Fields excluded: ``id``, ``response_metadata``, ``additional_kwargs``,
``usage_metadata``, ``name`` on non-tool-messages.

These excluded fields can drift between pre-checkpoint and post-checkpoint
objects (different defaultdict ordering, stripped metadata, tool-call ID
re-assignment).  Using an explicit allow-list guarantees that
``estimate_tokens(msgs_before) == estimate_tokens(msgs_after)`` after a
checkpoint round-trip, so the compaction trigger boundary does not drift
across resume and the KV-cache prefix is never invalidated by a phantom
threshold crossing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialization helpers — deterministic, allow-list based
# ---------------------------------------------------------------------------


def _extract_text_content(content: Any) -> str:
    """Flatten message content to a plain string (handles block-list format).

    Only ``text`` blocks are extracted from block-list content.  Non-text
    blocks (``tool_use``, ``image``, etc.) are silently skipped — they have
    negligible impact on token estimates and would introduce non-determinism
    if their internal structure changes across checkpoint cycles.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _serialize_for_token_count(messages: list[BaseMessage]) -> str:
    """Produce a deterministic, allow-list serialization of a message list.

    The output is a compact JSON string suitable for passing to a tokenizer
    or for byte-count heuristics.  Only fields that are semantically stable
    across checkpoint round-trips are included.

    Allow-list per message type
    ---------------------------
    All messages:
        ``type`` (str)
        ``content`` (flattened to text)

    AIMessage additionally:
        ``tool_calls[].name`` (str)
        ``tool_calls[].args`` (dict, JSON-serialized with sorted keys)

    ToolMessage additionally:
        ``tool_call_id`` (str)

    Excluded from all messages:
        ``id``, ``response_metadata``, ``additional_kwargs``,
        ``usage_metadata``, ``name`` (non-tool), ``invalid_tool_calls``

    Args:
        messages: Message list from graph state or compaction pipeline.

    Returns:
        A deterministic UTF-8 string representing the message list.
    """
    records: list[dict[str, Any]] = []
    for msg in messages:
        rec: dict[str, Any] = {
            "type": msg.type,
            "content": _extract_text_content(msg.content),
        }

        # AIMessage: include tool_calls with only name + args (sorted keys)
        if isinstance(msg, AIMessage) and msg.tool_calls:
            tc_list: list[dict[str, Any]] = []
            for call in msg.tool_calls:
                # Normalize: LangChain 0.3+ uses dict-typed entries
                call_dict: dict[str, Any] = dict(call) if isinstance(call, dict) else dict(call)
                name = call_dict.get("name", "unknown")
                args = call_dict.get("args", {})
                # Sort keys for deterministic JSON output
                tc_list.append({
                    "name": name,
                    "args": json.loads(json.dumps(args, sort_keys=True)),
                })
            rec["tool_calls"] = tc_list

        # ToolMessage: include the call_id for context length estimation
        if isinstance(msg, ToolMessage):
            rec["tool_call_id"] = msg.tool_call_id or ""

        records.append(rec)

    return json.dumps(records, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Anthropic client factory (lazy import, cached per process)
# ---------------------------------------------------------------------------

_anthropic_client = None


def _get_anthropic_client():
    """Lazily construct and cache an Anthropic client for count_tokens calls."""
    global _anthropic_client
    if _anthropic_client is None:
        try:
            import anthropic  # noqa: PLC0415 (lazy)
            _anthropic_client = anthropic.Anthropic()
        except Exception:
            _anthropic_client = None
    return _anthropic_client


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def estimate_tokens(messages: list[BaseMessage], provider: str) -> int:
    """Estimate the token count for a list of messages using the best available method.

    Provider dispatch
    -----------------
    * ``"anthropic"`` — uses ``anthropic.Anthropic().count_tokens(serialized)``.
      Falls back to heuristic if the SDK is not installed or raises.
    * ``"openai"``    — uses ``tiktoken.get_encoding("cl100k_base").encode(serialized)``.
      Falls back to heuristic if tiktoken is not installed or raises.
    * anything else   — ``len(serialized_utf8_bytes) // 3`` heuristic.
      Tolerance ±30%; acceptable for Gemini, BYOT, and unknown providers.

    Args:
        messages: The message list to estimate.  May be empty (returns 0).
        provider: The LLM provider string from ``agent_config.provider``.

    Returns:
        Estimated token count as a non-negative integer.
    """
    if not messages:
        return 0

    serialized = _serialize_for_token_count(messages)
    serialized_bytes = serialized.encode("utf-8")

    # -----------------------------------------------------------------------
    # Anthropic: use SDK count_tokens
    # -----------------------------------------------------------------------
    if provider == "anthropic":
        try:
            import anthropic as _anthropic  # noqa: PLC0415 (lazy)
            client = _get_anthropic_client()
            if client is not None:
                return int(client.count_tokens(serialized))
        except Exception:
            logger.debug(
                "estimate_tokens: anthropic count_tokens failed; falling back to heuristic",
                exc_info=True,
            )
        # Fall through to heuristic

    # -----------------------------------------------------------------------
    # OpenAI: use tiktoken
    # -----------------------------------------------------------------------
    elif provider == "openai":
        try:
            import tiktoken  # noqa: PLC0415 (lazy)
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(serialized))
        except Exception:
            logger.debug(
                "estimate_tokens: tiktoken encoding failed; falling back to heuristic",
                exc_info=True,
            )
        # Fall through to heuristic

    # -----------------------------------------------------------------------
    # Heuristic fallback — char/3 (covers Gemini, BYOT, unknown, fallthrough)
    # -----------------------------------------------------------------------
    return max(0, len(serialized_bytes) // 3)
