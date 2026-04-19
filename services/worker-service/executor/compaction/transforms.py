"""Track 7 — Compaction transforms.

Pure, deterministic, immutable transforms applied to the message list before
each LLM call.

Each transform:
- Never mutates the input ``messages`` list or any message in it.
- Returns the original list verbatim when no work is done (cache-stability).
- Advances a monotone watermark that gates which messages are candidates.

Task 5 adds ``clear_tool_results`` / ``ClearResult``.
Task 6 adds ``truncate_tool_call_args`` / ``TruncateResult``.

See docs/design-docs/phase-2/track-7-context-window-management.md §Tier 1.5
for the Tier 1.5 design rationale and algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage


# ---------------------------------------------------------------------------
# TruncateResult — Tier 1.5 return type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TruncateResult:
    """Result of ``truncate_tool_call_args``.

    Attributes:
        messages: The (possibly rewritten) message list. Identical to the
            input list when no truncation occurred.
        new_truncated_args_through_turn_index: Updated watermark. Monotone —
            always >= the input ``truncated_args_through_turn_index``.
        args_truncated: Number of individual arg values that were replaced
            with a placeholder in this call.
        bytes_saved: Total bytes removed (orig_bytes - placeholder_bytes)
            across all truncated args.
    """

    messages: list[BaseMessage]
    new_truncated_args_through_turn_index: int
    args_truncated: int
    bytes_saved: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _already_truncated(val: str) -> bool:
    """Return True if ``val`` looks like a previously emitted placeholder.

    Detection rule: starts with '[' AND contains the canonical marker string.
    This prevents re-truncation of placeholders from an earlier pass.
    """
    return val.startswith("[") and " bytes \u2014 arg truncated after step" in val


def _rebuild_tool_call(call: dict[str, Any], new_args: dict[str, Any]) -> dict[str, Any]:
    """Return a new tool_call dict with ``args`` replaced by ``new_args``.

    LangChain 0.3+ represents tool_calls as dicts with keys:
    ``id``, ``name``, ``args``, ``type``.  We construct a fresh dict so the
    original is never mutated.
    """
    return {**call, "args": new_args}


def _rebuild_ai_message(msg: AIMessage, new_tool_calls: list[dict[str, Any]]) -> AIMessage:
    """Return a new AIMessage with ``tool_calls`` replaced.

    Uses ``model_copy(update=...)`` (Pydantic v2 / LangChain 0.3+) which
    produces a shallow copy with the specified fields overridden.  The
    original ``msg`` is never mutated.
    """
    return msg.model_copy(update={"tool_calls": new_tool_calls})


# ---------------------------------------------------------------------------
# Tier 1.5 transform — tool-call argument truncation
# ---------------------------------------------------------------------------

def truncate_tool_call_args(
    messages: list[BaseMessage],
    truncated_args_through_turn_index: int,
    keep: int,
    truncatable_keys: frozenset[str],
    cap_bytes: int,
) -> TruncateResult:
    """Replace large string args in old AIMessage.tool_calls with placeholders.

    Only ``AIMessage`` instances with non-empty ``tool_calls`` are candidates.
    Other message types are passed through verbatim.

    Protection window: the most recent ``keep`` ToolMessage positions define
    a boundary index ``protect_from_index``.  Only AIMessages at an index
    strictly less than ``protect_from_index`` are candidates.

    Monotone watermark: ``new_truncated_args_through_turn_index`` is always
    ``max(truncated_args_through_turn_index, protect_from_index)``.

    No-op case: if the computed watermark equals the input watermark (nothing
    new to process), the original ``messages`` list is returned verbatim.

    Args:
        messages: Full message list from graph state.
        truncated_args_through_turn_index: Current watermark from graph state.
            Candidates are messages at indices < max(watermark, protect_from).
        keep: Number of most-recent ToolMessage turns to protect from
            truncation.
        truncatable_keys: Set of arg key names eligible for truncation.
        cap_bytes: An arg value whose UTF-8 byte length exceeds this threshold
            is replaced with a placeholder.

    Returns:
        A ``TruncateResult`` with the rewritten message list, updated watermark,
        and truncation counts.
    """
    # Collect positions of all ToolMessages in the list.
    tool_msg_positions = [
        i for i, m in enumerate(messages) if isinstance(m, ToolMessage)
    ]

    # If there are not more ToolMessages than keep, every tool turn is in the
    # protection window — nothing to truncate.
    if len(tool_msg_positions) <= keep:
        return TruncateResult(
            messages=messages,
            new_truncated_args_through_turn_index=truncated_args_through_turn_index,
            args_truncated=0,
            bytes_saved=0,
        )

    # The protection boundary: the position of the oldest ToolMessage still in
    # the "keep" window.  Candidate AIMessages are those at index < this value.
    if keep > 0:
        protect_from_index = tool_msg_positions[-keep]
    else:
        # keep=0 means protect from the first ToolMessage position
        protect_from_index = tool_msg_positions[0]

    new_watermark = max(truncated_args_through_turn_index, protect_from_index)

    # If the watermark did not advance, there is nothing new to process.
    if new_watermark == truncated_args_through_turn_index:
        return TruncateResult(
            messages=messages,
            new_truncated_args_through_turn_index=truncated_args_through_turn_index,
            args_truncated=0,
            bytes_saved=0,
        )

    # Build a mutable copy of the messages list (shallow — we only replace
    # individual elements when we touch them).
    compacted: list[BaseMessage] = list(messages)
    args_truncated = 0
    bytes_saved = 0

    for i, m in enumerate(messages):
        # Skip messages at or beyond the watermark (protected / future).
        if i >= new_watermark:
            continue

        # Only AIMessages with tool_calls are candidates.
        if not isinstance(m, AIMessage) or not m.tool_calls:
            continue

        new_tool_calls: list[dict[str, Any]] = []
        touched = False

        for call in m.tool_calls:
            # Normalise: LangChain 0.3+ uses dict-typed ToolCall entries.
            call_dict: dict[str, Any] = dict(call) if isinstance(call, dict) else dict(call)
            new_args: dict[str, Any] = dict(call_dict.get("args", {}))

            for key in list(new_args.keys()):
                val = new_args[key]

                # Only string values are eligible.
                if not isinstance(val, str):
                    continue

                # Only configured truncatable keys.
                if key not in truncatable_keys:
                    continue

                val_bytes = len(val.encode("utf-8"))

                # Only values above the byte threshold.
                if val_bytes <= cap_bytes:
                    continue

                # Skip already-truncated placeholders (idempotency).
                if _already_truncated(val):
                    continue

                # Replace with placeholder.
                placeholder = (
                    f"[{val_bytes} bytes \u2014 arg truncated after step {i}]"
                )
                new_args[key] = placeholder
                placeholder_bytes = len(placeholder.encode("utf-8"))
                args_truncated += 1
                bytes_saved += val_bytes - placeholder_bytes
                touched = True

            new_tool_calls.append(_rebuild_tool_call(call_dict, new_args))

        if touched:
            compacted[i] = _rebuild_ai_message(m, new_tool_calls)

    return TruncateResult(
        messages=compacted,
        new_truncated_args_through_turn_index=new_watermark,
        args_truncated=args_truncated,
        bytes_saved=bytes_saved,
    )
