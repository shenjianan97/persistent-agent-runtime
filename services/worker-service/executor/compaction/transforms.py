"""Track 7 — Compaction transforms (Tier 1 and Tier 1.5).

Pure, deterministic, immutable transforms applied to the message list before
each LLM call.

Each transform:
- Never mutates the input ``messages`` list or any message in it.
- Returns the original list verbatim when no work is done (cache-stability).
- Advances a monotone watermark that gates which messages are candidates.

Task 5 adds ``clear_tool_results`` / ``ClearResult`` — Tier 1 tool-result clearing.
Task 6 adds ``truncate_tool_call_args`` / ``TruncateResult`` — Tier 1.5 arg truncation.

See docs/design-docs/phase-2/track-7-context-window-management.md §Tier 1
and §Tier 1.5 for the design rationale and contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage


# ---------------------------------------------------------------------------
# ClearResult — Tier 1 return type
# ---------------------------------------------------------------------------

_ALREADY_CLEARED_PREFIX = "[tool output not retained —"


@dataclass(frozen=True)
class ClearResult:
    """Return value of clear_tool_results.

    Attributes:
        messages: Compacted message view (never the same list as the input
            unless the operation was a no-op).
        new_cleared_through_turn_index: Monotone watermark after this pass.
            Always >= the input ``cleared_through_turn_index``.
        messages_cleared: How many ToolMessages were actually rewritten this
            pass (0 when no-op).
        est_tokens_saved: Rough estimate of tokens saved using the
            ``(byte_len_diff / 3.5)`` heuristic. 0 on no-op.
    """

    messages: list[BaseMessage]
    new_cleared_through_turn_index: int
    messages_cleared: int
    est_tokens_saved: int


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


def _already_cleared(content: Any) -> bool:
    """Return True if the content already starts with the cleared placeholder prefix."""
    if not isinstance(content, str):
        return False
    return content.startswith(_ALREADY_CLEARED_PREFIX)


def _build_tool_name_map(messages: list[BaseMessage]) -> dict[str, str]:
    """Build a mapping tool_call_id → tool_name from AIMessage.tool_calls in messages.

    LangChain 0.2+ represents tool_calls as a list of dicts:
        {"id": ..., "name": ..., "args": ..., "type": "tool_call"}
    This function supports both the dict shape and the occasional attribute-
    access shape for safety.
    """
    name_by_id: dict[str, str] = {}
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        for call in (m.tool_calls or []):
            if isinstance(call, dict):
                call_id: str | None = call.get("id")
                call_name: str | None = call.get("name")
            else:
                call_id = getattr(call, "id", None)
                call_name = getattr(call, "name", None)
            if call_id is not None and call_name is not None:
                name_by_id[call_id] = call_name
    return name_by_id


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
# Tier 1 transform — tool-result clearing
# ---------------------------------------------------------------------------


def clear_tool_results(
    messages: list[BaseMessage],
    cleared_through_turn_index: int,
    keep: int,
    exclude_tools_effective: frozenset[str],
) -> ClearResult:
    """Replace older ToolMessage content with a deterministic placeholder (Tier 1).

    Implements the Tier 1 "observation masking" transform described in
    docs/design-docs/phase-2/track-7-context-window-management.md §Tier 1.

    Args:
        messages: Full message list from graph state. Treated as immutable;
            a new list is returned on changes.
        cleared_through_turn_index: Current watermark from graph state.
            Messages at indices < this value have already been cleared in a
            prior pass and will not be re-evaluated.
        keep: Number of most-recent ToolMessage instances to protect from
            clearing (protection window).
        exclude_tools_effective: Union of platform exclude list and
            per-agent exclude list. ToolMessages whose name matches are
            never cleared regardless of age.

    Returns:
        ClearResult with the (possibly compacted) message list, updated
        watermark, and stats.
    """
    # Collect positions of all ToolMessage instances.
    tool_msg_positions = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]

    # If there are <= keep ToolMessages, nothing to clear.
    if len(tool_msg_positions) <= keep:
        return ClearResult(
            messages=messages,
            new_cleared_through_turn_index=cleared_through_turn_index,
            messages_cleared=0,
            est_tokens_saved=0,
        )

    # protect_from_index is the position of the KEEP-th most recent ToolMessage.
    # Everything at indices < protect_from_index is a candidate for clearing.
    protect_from_index = tool_msg_positions[-keep]

    # Monotone watermark: never allow regression.
    new_watermark = max(cleared_through_turn_index, protect_from_index)

    # If the watermark did not advance, no new work to do — return original.
    if new_watermark == cleared_through_turn_index:
        return ClearResult(
            messages=messages,
            new_cleared_through_turn_index=cleared_through_turn_index,
            messages_cleared=0,
            est_tokens_saved=0,
        )

    # Build tool_call_id → tool_name map for name recovery on ToolMessages
    # whose .name attribute is None.
    tool_name_by_call_id = _build_tool_name_map(messages)

    # Build new list (shallow copy; only replace ToolMessages that qualify).
    compacted = list(messages)
    cleared_count = 0
    tokens_saved_est = 0

    for i, m in enumerate(messages):
        # Only consider positions strictly before the protection boundary.
        if i >= new_watermark:
            continue
        if not isinstance(m, ToolMessage):
            continue

        # Resolve tool name: prefer .name attribute, fall back to AIMessage lookup.
        tool_name: str = m.name or tool_name_by_call_id.get(m.tool_call_id, "unknown_tool")

        # Skip excluded tools.
        if tool_name in exclude_tools_effective:
            continue

        # Skip already-cleared messages (idempotency + cache stability).
        if _already_cleared(m.content):
            continue

        # Non-string content: guard against encode() errors; skip clearing.
        if not isinstance(m.content, str):
            continue

        orig_bytes = len(m.content.encode("utf-8"))
        placeholder = (
            f"[tool output not retained — {tool_name} returned {orig_bytes} bytes at step {i}]"
        )

        compacted[i] = ToolMessage(
            content=placeholder,
            tool_call_id=m.tool_call_id,
            name=m.name,
        )
        cleared_count += 1
        # Token estimate: heuristic len_diff / 3.5, floored at 0.
        placeholder_bytes = len(placeholder.encode("utf-8"))
        tokens_saved_est += max(0, int((orig_bytes - placeholder_bytes) / 3.5))

    return ClearResult(
        messages=compacted,
        new_cleared_through_turn_index=new_watermark,
        messages_cleared=cleared_count,
        est_tokens_saved=tokens_saved_est,
    )


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
