<!-- AGENT_TASK_START: task-3-per-tool-result-cap.md -->

# Task 3 — Per-Tool-Result Cap at Ingestion

## Agent Instructions

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — section "Per-tool-result cap at ingestion".
2. `services/worker-service/executor/graph.py` lines around `_get_tools` and any `ToolNode` registration — understand where `ToolMessage.content` is produced today.
3. `services/worker-service/executor/compaction/defaults.py` (from Task 2) — import `PER_TOOL_RESULT_CAP_BYTES`.
4. `services/worker-service/tools/` — the tool wrapper style used today (look at `sandbox_tools.py`, `memory_tools.py`).
5. `services/worker-service/core/logging.py` — structured-log helpers.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make worker-test` and `make e2e-test` (DB-touching suite confirms no regression under a real task). Fix any regressions.
2. Update the status in `docs/exec-plans/active/phase-2/track-7/progress.md` to "Done".

## Context

Every `ToolMessage` entering graph state must be capped at `PER_TOOL_RESULT_CAP_BYTES` (25KB) bytes. Cap is **head + tail truncation** — the start and end of the output are preserved (stack-trace headers, command echoes, final result lines), only the middle is elided. Cap is enforced **at the tool wrapper**, not in the compaction pipeline. This guarantees:

- No oversized `ToolMessage` ever touches state → checkpoints stay small.
- Cache-stable: the capped string is what gets written and replayed; no subsequent transform changes it.
- Independent of Tier 1/1.5/3 — a pathological single tool call that lands inside the protection window cannot blow the context.

The cap applies universally: built-in tools (`sandbox_*`, `web_search`), BYOT MCP tools, memory tools (`memory_search`, `task_history_get`), human-input responses — every tool.

## Task-Specific Shared Contract

- `cap_tool_result(raw: str, tool_name: str) -> tuple[str, CapEvent | None]`:
  - Returns `(raw, None)` when `len(raw.encode("utf-8")) <= PER_TOOL_RESULT_CAP_BYTES`.
  - Otherwise returns `(capped, CapEvent(...))` with head + tail truncation and a human-readable middle marker.
- `CapEvent` is a small dataclass (or NamedTuple) with fields `tool: str`, `orig_bytes: int`, `capped_bytes: int`. Used for log emission.
- The head + tail split is `PER_TOOL_RESULT_CAP_BYTES // 2` bytes each, with a middle marker:
  `\n[... truncated {dropped_bytes} bytes. Tool returned {orig_bytes} bytes total; use a narrower query or smaller offset/limit to read the rest. ...]\n`
- Byte-accurate head/tail slicing. Cutting in the middle of a multi-byte UTF-8 codepoint is acceptable — the marker wraps it. Use `raw.encode("utf-8")[:N]` + `.decode("utf-8", errors="replace")` to avoid crashing on partial codepoints.
- Tool wrappers in `graph.py` (and any tool-building helpers) call `cap_tool_result` on the tool's return value before constructing the `ToolMessage`. Emit the structured log `compaction.per_result_capped` when `CapEvent` is non-None.

## Affected Component

- **Service/Module:** Worker Service — Compaction + tool wrappers
- **File paths:**
  - `services/worker-service/executor/compaction/caps.py` (new)
  - `services/worker-service/executor/graph.py` (modify — wrap every tool's return value through `cap_tool_result`)
  - **Do NOT edit `compaction/__init__.py`** — Task 7 owns its final shape. Import `cap_tool_result`, `CapEvent` directly from `executor.compaction.caps`.
  - `services/worker-service/tests/test_compaction_caps.py` (new)
  - `services/worker-service/tests/test_graph_tool_cap_integration.py` (new or extend an existing graph-test file)
- **Change type:** new module + modification of `graph.py` tool wrappers

## Dependencies

- **Must complete first:** Task 2 (imports `PER_TOOL_RESULT_CAP_BYTES`).
- **Provides output to:** Task 7 (pipeline relies on the cap having already been applied at ingestion).
- **Shared interfaces/contracts:** The `cap_tool_result` function signature and `CapEvent` type.

## Implementation Specification

### `caps.py`

```python
"""Per-tool-result byte cap at ingestion (Track 7 Tier 0 / hard floor).

See docs/design-docs/phase-2/track-7-context-window-management.md §Per-tool-result
cap at ingestion.
"""
from dataclasses import dataclass

from executor.compaction.defaults import PER_TOOL_RESULT_CAP_BYTES


@dataclass(frozen=True)
class CapEvent:
    tool: str
    orig_bytes: int
    capped_bytes: int


def cap_tool_result(raw: str, tool_name: str) -> tuple[str, CapEvent | None]:
    """Head+tail truncate a tool result if it exceeds the byte cap.

    Returns (raw, None) when within cap; otherwise (capped, CapEvent).
    """
    raw_bytes = raw.encode("utf-8")
    orig_bytes = len(raw_bytes)
    if orig_bytes <= PER_TOOL_RESULT_CAP_BYTES:
        return raw, None

    half = PER_TOOL_RESULT_CAP_BYTES // 2
    head = raw_bytes[:half].decode("utf-8", errors="replace")
    tail = raw_bytes[-half:].decode("utf-8", errors="replace")
    dropped = orig_bytes - (2 * half)
    marker = (
        f"\n[... truncated {dropped} bytes. "
        f"Tool returned {orig_bytes} bytes total; use a narrower query or "
        f"smaller offset/limit to read the rest. ...]\n"
    )
    capped = f"{head}{marker}{tail}"
    return capped, CapEvent(
        tool=tool_name,
        orig_bytes=orig_bytes,
        capped_bytes=len(capped.encode("utf-8")),
    )
```

### `graph.py` integration

For every tool registered via the `_get_tools` path (both built-in and MCP-proxied), wrap the tool's return to apply the cap. The cleanest pattern is a helper decorator:

```python
from executor.compaction.caps import cap_tool_result
from core.logging import log_structured

def _apply_result_cap(tool_name: str):
    def decorator(fn):
        async def wrapper(*args, **kwargs):
            result = await fn(*args, **kwargs)
            if not isinstance(result, str):
                result_str = str(result)
            else:
                result_str = result
            capped, event = cap_tool_result(result_str, tool_name)
            if event is not None:
                log_structured(
                    "compaction.per_result_capped",
                    tool=event.tool,
                    orig_bytes=event.orig_bytes,
                    capped_bytes=event.capped_bytes,
                    # include tenant_id, agent_id, task_id from the surrounding
                    # closure variables already available in _get_tools
                )
            return capped
        return wrapper
    return decorator
```

Apply this decorator to every tool returned by `_get_tools` so the cap happens before the `ToolNode` constructs the `ToolMessage`. For MCP tools (Track 4), cap the result inside the existing MCP-call wrapper — the wrapper already stringifies the MCP response before returning it to LangGraph.

**Do NOT** apply the cap inside `_handle_tool_error` — errors are small and should not be truncated.

**Do NOT** cap the tool's input — this task is result-cap only.

### Langfuse annotation

When `cap_tool_result` fires, in addition to the structured log, emit an annotation on the parent tool span (if a Langfuse callback is active). The annotation key is `"compaction.per_result_capped"` with the same payload. Reuse the existing `_build_langfuse_callback` pattern — callbacks observe tool ends and can annotate them there.

## Acceptance Criteria

- [ ] `cap_tool_result("hello", "web_search")` returns `("hello", None)`.
- [ ] `cap_tool_result("x" * 30_000, "web_search")` returns a capped string strictly shorter than the original and a non-None `CapEvent` with `orig_bytes=30_000`.
- [ ] Capped output begins with the first 12,500 bytes and ends with the last 12,500 bytes of the original.
- [ ] Middle marker contains the byte counts (`orig_bytes` and `dropped`).
- [ ] `cap_tool_result` handles UTF-8 multi-byte boundaries without raising (assert on a payload with `"日"` characters near the cut points).
- [ ] Every tool registered in `_get_tools` applies the cap decorator — grep-test that asserts `@_apply_result_cap` or equivalent wraps each tool function.
- [ ] Integration test: a built-in tool returning a 500KB string produces a `ToolMessage` with `len(content) ≤ ~25K` after the full execution path; `compaction.per_result_capped` is logged once.
- [ ] Integration test: an error path (`_handle_tool_error`) is NOT affected by the cap.
- [ ] Unit tests pass on `make worker-test`.

## Testing Requirements

- **Unit tests for `caps.py`:** under-cap passes through; over-cap head+tail structure; byte-exact sizes; UTF-8 boundary safety; tool_name is echoed in `CapEvent`.
- **Integration tests:** build a synthetic tool that returns > 25KB; run it through the `_get_tools` wrapping path; assert the `ToolMessage` content is capped and the log line fired.
- **MCP tool integration (if Track 4 code paths are touched):** one integration test where a mock MCP server returns 500KB confirms the cap fires.
- **No regression on short results:** confirm a 1KB `sandbox_read_file` result is unchanged.

## Constraints and Guardrails

- Do not mutate `PER_TOOL_RESULT_CAP_BYTES` at runtime.
- Do not cap tool inputs (that is Tier 1.5's job, and only for old turns).
- Do not cap error-path tool outputs (`_handle_tool_error`).
- Do not apply the cap twice (idempotent in theory, but once is correct).
- Byte-accurate slicing, not codepoint-accurate — the marker accounts for any partial codepoint.
- Do not rely on the cap at read time in later tasks — Tasks 4/5/7 work on already-capped messages.

## Assumptions

- Tool return values are always strings (or stringifiable). If an MCP tool returns structured JSON, the existing wrapper already serializes it to a string before LangGraph sees it.
- `log_structured` is the worker's existing structured-logger helper. If not present, use the worker's existing JSON-logger pattern (look at how Track 5's `memory.write.committed` event is emitted).
- LangGraph's `ToolNode` constructs `ToolMessage(content=<return value>, tool_call_id=...)` — we cap the return value before it reaches `ToolNode`.

<!-- AGENT_TASK_END: task-3-per-tool-result-cap.md -->
