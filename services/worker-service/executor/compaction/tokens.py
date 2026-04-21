"""Token estimation for Track 7 compaction pipeline.

Entry point: :func:`estimate_tokens`.

Uses the real tokenizer when available (tiktoken for OpenAI) and falls back
to a ``len(serialized) // 3`` heuristic for Anthropic, Gemini, BYOT, and any
unknown provider. The heuristic is canonical for Anthropic because the
legacy ``anthropic.Anthropic().count_tokens()`` entry point was removed from
modern SDKs (≥ 0.40); the replacement ``client.messages.count_tokens`` is
async and requires a model id, so migrating to it means threading `async` +
model name through the hook — a larger change deferred to a future PR.

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

langchain-core upgrade caveat
-----------------------------
``extract_text_content`` delegates to ``AIMessage.content_blocks``, whose
provider translator set is still documented as "alpha" upstream. A minor
langchain-core bump that adds a translator (e.g., folds ``thinking`` into
a standard ``text`` block, or unwraps the OpenAI Responses ``message``
wrapper natively) will change what the helper extracts for the same
logical message. During a rolling deploy this means a task resumed on a
newer worker can estimate a different token count than its pre-checkpoint
self, shifting the compaction trigger boundary across the restart. This
is not a correctness bug for a single run — but pin ``langchain-core``
tightly in ``pyproject.toml`` and coordinate the bump fleet-wide. The
allow-listed scalar fields above remain deterministic regardless.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

# Re-exported under its legacy underscore name to keep intra-module callers
# (summarizer.py) stable, but the public surface is `extract_text_content`.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialization helpers — deterministic, allow-list based
# ---------------------------------------------------------------------------


def extract_text_content(content: Any, *, separator: str = "") -> str:
    """Flatten provider-shaped message content to plain text.

    Provider gap (verified against ``langchain-core==1.3.0``, 2026-04-17):

    * ``BaseMessage.text`` is NOT a canonical cross-provider flattener. It
      reads ``self.content`` directly and only picks ``{"type": "text",
      "text": ...}`` blocks — it does not dispatch to provider translators,
      does not consult ``content_blocks``, and returns ``""`` for OpenAI
      Responses ``output_text``, nested ``message.content[output_text]``,
      Gemini / Bedrock Converse bare-dict, and anything else provider-shaped.
    * ``AIMessage.content_blocks`` DOES dispatch to per-provider translators
      (when ``response_metadata["model_provider"]`` is set) and runs a
      best-effort pass otherwise, which normalizes Anthropic text, Gemini
      bare-dict, and Bedrock Converse bare-dict into standard
      ``{"type": "text"}`` blocks. OpenAI Responses shapes are still left as
      ``"non_standard"``-wrapped blocks (open gap tracked upstream — see the
      forum thread "Why open ai reasoning content is not parsed into
      standard content blocks" and issues #9072 / #9895).

    Strategy:

    1. Call ``AIMessage(content=content).content_blocks`` so every provider
       translator LangChain ships with runs.
    2. Extract text from standard ``"text"`` blocks.
    3. Peek one level into ``"non_standard"`` wrappers for the shapes we
       know LangChain doesn't yet normalize: ``output_text`` (OpenAI
       Responses), nested ``message.content[...]`` (OpenAI Responses wrap),
       and ``thinking`` (Claude extended thinking). Narrow allowlist, not a
       reimplementation of the translator stack.

    When a new provider ships or the ``content_blocks`` alpha stabilizes and
    folds a shape into standard ``"text"`` blocks, the ``non_standard`` pass
    here becomes a no-op and can be trimmed.

    Non-text blocks (``tool_use``, ``function_call``, ``reasoning``,
    ``image``, ...) are intentionally dropped from the text view — they do
    not contribute to the summarizer prompt or the token-count heuristic.

    Separator — programmatic vs user-facing
    ---------------------------------------
    ``separator`` defaults to ``""`` because the historical / token-count
    / summarizer-input callers treat sibling text blocks as programmatic
    concatenation (not paragraph breaks). For user-facing artifacts —
    specifically ``task.output.result`` written at graph completion —
    callers should pass ``separator="\\n\\n"`` so Anthropic multi-block
    responses render with paragraph spacing, matching the Java read-time
    normalizer (``MessageContentExtractor`` in the API service) and
    preserving markdown structure (adjacent ``## Heading`` blocks don't
    collapse into the previous block's last line).

    Exception handling: a failure inside ``content_blocks`` (e.g., a future
    LangChain shape raising during best-effort parsing) is logged at
    WARNING and degrades to ``""``. Silent swallowing would make a prod
    regression invisible — the WARNING-level log is the signal.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        blocks = AIMessage(content=content).content_blocks
    except Exception:
        logger.warning(
            "extract_text_content: content_blocks raised; degrading to empty",
            exc_info=True,
        )
        return ""
    parts: list[str] = []
    for block in blocks:
        part = _extract_block_text(block)
        if part:
            parts.append(part)
    return separator.join(parts)


# Legacy alias — kept so `summarizer.py` and other in-tree callers importing
# the underscore name don't need an atomic rewrite. New callers use
# ``extract_text_content`` directly.
_extract_text_content = extract_text_content


def _extract_block_text(block: Any) -> str:
    """Pull prose text out of a single ``content_blocks`` entry.

    Standard ``"text"`` blocks yield their ``"text"`` field. ``"non_standard"``
    wrappers are unpacked one level for shapes LangChain's v1 content_blocks
    pipeline leaves as opaque envelopes today — namely OpenAI Responses
    ``output_text``, the nested ``message.content[...]`` wrapper, and
    Claude extended thinking. Everything else yields ``""`` (including
    ``tool_call``, ``reasoning``, ``image``, etc., which are surfaced via
    their own channels, not prose).
    """
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    block_type = block.get("type")
    if block_type == "text":
        text = block.get("text")
        return text if isinstance(text, str) else ""
    if block_type == "non_standard":
        inner = block.get("value")
        if isinstance(inner, dict):
            inner_type = inner.get("type")
            if inner_type == "output_text":
                text = inner.get("text")
                return text if isinstance(text, str) else ""
            if inner_type == "message":
                nested = inner.get("content")
                if isinstance(nested, list):
                    return _extract_text_content(nested)
            if inner_type == "thinking":
                thinking = inner.get("thinking")
                return thinking if isinstance(thinking, str) else ""
    return ""


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
# Main entry point
# ---------------------------------------------------------------------------


def estimate_tokens(messages: list[BaseMessage], provider: str) -> int:
    """Estimate the token count for a list of messages using the best available method.

    Provider dispatch
    -----------------
    * ``"openai"``  — uses ``tiktoken.get_encoding("cl100k_base").encode(serialized)``.
      Falls back to heuristic if tiktoken is not installed or raises.
    * everything else (``"anthropic"``, Gemini, BYOT, unknown) —
      ``len(serialized_utf8_bytes) // 3`` heuristic. Tolerance ±30%.

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
    # OpenAI: use tiktoken
    # -----------------------------------------------------------------------
    if provider == "openai":
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
    # Heuristic fallback — char/3 (anthropic, Gemini, BYOT, unknown)
    # -----------------------------------------------------------------------
    return max(0, len(serialized_bytes) // 3)
