"""Per-tool-result byte cap at ingestion (Track 7 Tier 0 / hard floor).

See docs/design-docs/phase-2/track-7-context-window-management.md §Per-tool-result
cap at ingestion.

Every ToolMessage entering graph state is capped at PER_TOOL_RESULT_CAP_BYTES
(25 KB) before it touches state. This guarantees:

- No oversized ToolMessage ever enters LangGraph state → checkpoints stay small.
- Cache-stable: the capped string is written and replayed; no subsequent
  transform changes it.
- Independent of Tier 1/1.5/3 — a pathological single tool call inside the
  protection window cannot blow the context.

The cap is applied universally: built-in tools (sandbox_*, web_search),
BYOT MCP tools, memory tools (memory_search, task_history_get), and
human-input responses — every tool.
"""
from dataclasses import dataclass

from executor.compaction.defaults import PER_TOOL_RESULT_CAP_BYTES


@dataclass(frozen=True)
class CapEvent:
    """Emitted when cap_tool_result truncates a tool result.

    Attributes:
        tool: Name of the tool whose result was capped.
        orig_bytes: UTF-8 byte length of the raw result before capping.
        capped_bytes: UTF-8 byte length of the capped result.
    """

    tool: str
    orig_bytes: int
    capped_bytes: int


def cap_tool_result(raw: str, tool_name: str) -> tuple[str, CapEvent | None]:
    """Head+tail truncate a tool result if it exceeds the byte cap.

    Returns (raw, None) when within cap; otherwise (capped, CapEvent).

    The total output byte length is guaranteed ``<= PER_TOOL_RESULT_CAP_BYTES``
    (hard cap). Marker bytes are reserved from the head/tail allocation
    rather than added on top.

    Algorithm:
        1. Encode raw to bytes. If ``<= PER_TOOL_RESULT_CAP_BYTES``, return as-is.
        2. Upper-bound the marker byte length using orig_bytes (worst case),
           compute remaining budget, split evenly between head and tail.
        3. Decode head and tail bytes with ``errors="replace"`` to tolerate
           partial multi-byte codepoints at slice boundaries.
        4. Build the marker with the exact dropped-byte count.
        5. Defensive trim: if UTF-8 replacement characters caused a small
           overrun, trim the tail to enforce the hard-cap invariant.

    Args:
        raw: The raw tool result string (may be arbitrarily large).
        tool_name: Name of the tool that produced this result (for CapEvent).

    Returns:
        A ``(result, event)`` tuple. ``event`` is ``None`` when no cap was applied.
    """
    raw_bytes = raw.encode("utf-8")
    orig_bytes = len(raw_bytes)
    if orig_bytes <= PER_TOOL_RESULT_CAP_BYTES:
        return raw, None

    # Build the marker first (using the deterministic dropped-byte count
    # computed AFTER head/tail sizes are known — see below), then reserve
    # its byte length from the cap so head + marker + tail fits the cap.
    # We upper-bound marker length with the final values since `dropped`
    # depends on the final head/tail sizes. Simplest stable formulation:
    # size the marker using the worst-case dropped count (orig_bytes),
    # then allocate head/tail from the remaining budget.
    max_marker = (
        f"\n[... truncated {orig_bytes} bytes. "
        f"Tool returned {orig_bytes} bytes total; use a narrower query or "
        f"smaller offset/limit to read the rest. ...]\n"
    )
    reserve = len(max_marker.encode("utf-8"))
    budget = max(0, PER_TOOL_RESULT_CAP_BYTES - reserve)
    half = budget // 2
    head = raw_bytes[:half].decode("utf-8", errors="replace")
    tail = raw_bytes[-half:].decode("utf-8", errors="replace") if half > 0 else ""
    dropped = orig_bytes - (2 * half)
    marker = (
        f"\n[... truncated {dropped} bytes. "
        f"Tool returned {orig_bytes} bytes total; use a narrower query or "
        f"smaller offset/limit to read the rest. ...]\n"
    )
    capped = f"{head}{marker}{tail}"
    # Defensive: if UTF-8 replace characters caused a small overrun, trim
    # to keep the hard-cap invariant true.
    #
    # Use errors="ignore" (not "replace") for the final defensive trim so
    # that an incomplete multi-byte codepoint at the boundary is silently
    # dropped rather than replaced by U+FFFD (which is 3 bytes and would
    # push a result that is exactly 1–3 bytes over the cap back over again).
    # "ignore" guarantees: len(result.encode("utf-8")) <= PER_TOOL_RESULT_CAP_BYTES
    # on the first attempt, with no looping required.
    capped_encoded = capped.encode("utf-8")
    if len(capped_encoded) > PER_TOOL_RESULT_CAP_BYTES:
        capped = capped_encoded[:PER_TOOL_RESULT_CAP_BYTES].decode(
            "utf-8", errors="ignore"
        )
    return capped, CapEvent(
        tool=tool_name,
        orig_bytes=orig_bytes,
        capped_bytes=len(capped.encode("utf-8")),
    )
