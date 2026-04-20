"""Track 7 — Compaction transforms (Tier 1).

Pure, deterministic, immutable transforms applied to the message list before
each LLM call.

Each transform:
- Never mutates the input ``messages`` list or any message in it.
- Returns the original list verbatim when no work is done (cache-stability).
- Advances a monotone watermark that gates which messages are candidates.

``clear_tool_results`` / ``ClearResult`` — Tier 1 tool-result clearing.

Tier 1.5 (``truncate_tool_call_args``) was removed in the Track 7 Follow-up
(Task 4). Its role is now played at ingestion time by
:func:`executor.compaction.ingestion.offload_ai_message_args`.

See docs/design-docs/phase-2/track-7-context-window-management.md §Tier 1
for the design rationale and contract.
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
# Internal helpers
# ---------------------------------------------------------------------------
#
# Tier 1.5 ``truncate_tool_call_args`` / ``TruncateResult`` were removed in the
# Track 7 Follow-up (Task 4). Ingestion-time offload (see
# ``executor/compaction/ingestion.py``) replaces reactive arg truncation at
# compaction time.


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


# NOTE: Tier 1.5 ``truncate_tool_call_args`` / ``TruncateResult`` were removed
# in the Track 7 Follow-up (Task 4). The replacement is
# :func:`executor.compaction.ingestion.offload_ai_message_args`, which offloads
# oversized truncatable arg values to S3 at AIMessage-append time.
