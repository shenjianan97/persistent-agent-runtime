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

For agents with Track 7 enabled, every `ToolMessage` entering graph state must be capped at `PER_TOOL_RESULT_CAP_BYTES` (25KB) bytes. Cap is **head + tail truncation** — the start and end of the output are preserved (stack-trace headers, command echoes, final result lines), only the middle is elided. Cap is enforced **at the tool wrapper**, not in the compaction pipeline. This guarantees:

- No oversized `ToolMessage` ever touches state → checkpoints stay small.
- Cache-stable: the capped string is what gets written and replayed; no subsequent transform changes it.
- Independent of Tier 1/1.5/3 — a pathological single tool call that lands inside the protection window cannot blow the context.

For agents with Track 7 disabled (`context_management.enabled=false`, or the rollout-resolved effective default is `false`), the cap MUST NOT fire. Design Validation #8 ("compaction-disabled agents behave identically to pre-Track-7") is a correctness gate — a disabled agent must see its full, verbatim tool output in state exactly as pre-Track-7. The tool wrapper therefore consults the effective-enabled flag on every call and skips the cap when compaction is off for the agent.

When Track 7 is enabled, the cap applies universally across: built-in tools (`sandbox_*`, `web_search`), BYOT MCP tools, memory tools (`memory_search`, `task_history_get`), human-input responses — every tool.

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

- **Must complete first:** Task 2 (imports `PER_TOOL_RESULT_CAP_BYTES` and `effective_context_management_enabled`).
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

    The total output byte length is guaranteed `<= PER_TOOL_RESULT_CAP_BYTES`
    (hard cap). Marker bytes are reserved from the head/tail allocation
    rather than added on top.
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
    tail = raw_bytes[-half:].decode("utf-8", errors="replace")
    dropped = orig_bytes - (2 * half)
    marker = (
        f"\n[... truncated {dropped} bytes. "
        f"Tool returned {orig_bytes} bytes total; use a narrower query or "
        f"smaller offset/limit to read the rest. ...]\n"
    )
    capped = f"{head}{marker}{tail}"
    # Defensive: if UTF-8 replace characters caused a small overrun, trim
    # the tail to keep the hard-cap invariant true.
    capped_encoded = capped.encode("utf-8")
    if len(capped_encoded) > PER_TOOL_RESULT_CAP_BYTES:
        capped = capped_encoded[:PER_TOOL_RESULT_CAP_BYTES].decode(
            "utf-8", errors="replace"
        )
    return capped, CapEvent(
        tool=tool_name,
        orig_bytes=orig_bytes,
        capped_bytes=len(capped.encode("utf-8")),
    )
```

**Hard-cap invariant:** `len(capped.encode("utf-8")) <= PER_TOOL_RESULT_CAP_BYTES` for all inputs, including pathological ones (UTF-8 replacement expansion, tiny cap values). Enforced by the final defensive trim. Unit test covers a cap value smaller than the marker itself — the function returns an empty-head, empty-tail, marker-only (possibly truncated) string, still respecting the cap.

### `graph.py` integration

For every tool registered via the `_get_tools` path (both built-in and MCP-proxied), wrap the tool's return to apply the cap. The cleanest pattern is a helper decorator:

```python
from executor.compaction.caps import cap_tool_result
from core.logging import log_structured

def _apply_result_cap(tool_name: str, *, compaction_enabled: bool):
    """Wraps a tool so its return value is head+tail capped when compaction
    is enabled for this agent. When compaction is disabled, the wrapper is
    a pass-through — the tool's return is handed to the ToolNode verbatim,
    preserving pre-Track-7 behavior exactly (Design Validation #8).
    """
    def decorator(fn):
        async def wrapper(*args, **kwargs):
            result = await fn(*args, **kwargs)
            if not compaction_enabled:
                return result
            result_str = result if isinstance(result, str) else str(result)
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

Apply this decorator to every tool returned by `_get_tools`, passing `compaction_enabled=effective_context_management_enabled(agent_config, ...)` (the same worker-side resolver Task 7 will expose). For MCP tools (Track 4), wrap the existing MCP-call wrapper the same way. The cap happens before the `ToolNode` constructs the `ToolMessage`.

**Important:** `effective_context_management_enabled` is a pure function of `agent_config`, the agent's `created_at`, and rollout env vars. Task 7 owns its exact shape; Task 3 takes it as an input (pass the resolved boolean down into `_get_tools` from wherever it's already computed at graph-build time). Task 3 does not duplicate the resolver logic.

**Do NOT** apply the cap inside `_handle_tool_error` — errors are small and should not be truncated.

**Do NOT** cap the tool's input — this task is result-cap only.

### Langfuse annotation

When `cap_tool_result` fires, in addition to the structured log, emit an annotation on the parent tool span (if a Langfuse callback is active). The annotation key is `"compaction.per_result_capped"` with the same payload. Reuse the existing `_build_langfuse_callback` pattern — callbacks observe tool ends and can annotate them there.

## Acceptance Criteria

- [ ] `cap_tool_result("hello", "web_search")` returns `("hello", None)`.
- [ ] `cap_tool_result("x" * 30_000, "web_search")` returns a capped string strictly shorter than the original and a non-None `CapEvent` with `orig_bytes=30_000`.
- [ ] Capped output's total UTF-8 byte length is `<= PER_TOOL_RESULT_CAP_BYTES` for every input, including pathological cases — no output exceeds the hard cap by even one byte.
- [ ] Head and tail together consume approximately `PER_TOOL_RESULT_CAP_BYTES - len(marker)` bytes; head and tail each get roughly half that budget. Test on a 500KB input asserts head ≈ tail ≈ (cap - marker_bytes) / 2.
- [ ] Middle marker contains the byte counts (`orig_bytes` and `dropped`).
- [ ] `cap_tool_result` handles UTF-8 multi-byte boundaries without raising (assert on a payload with `"日"` characters near the cut points).
- [ ] Every tool registered in `_get_tools` applies the cap decorator — grep-test that asserts `@_apply_result_cap` or equivalent wraps each tool function.
- [ ] Integration test (compaction enabled): a built-in tool returning a 500KB string produces a `ToolMessage` with `len(content) ≤ PER_TOOL_RESULT_CAP_BYTES` after the full execution path; `compaction.per_result_capped` is logged once.
- [ ] **Integration test (compaction disabled)**: same 500KB tool result on an agent with `context_management.enabled=false` lands in the `ToolMessage.content` VERBATIM (byte-identical to the original). No `compaction.per_result_capped` log emitted. Regression gate — this is the pre-Track-7 parity contract.
- [ ] Integration test: an error path (`_handle_tool_error`) is NOT affected by the cap.
- [ ] Unit tests pass on `make worker-test`.

## Testing Requirements

- **Unit tests for `caps.py`:** under-cap passes through; over-cap head+tail structure; byte-exact sizes; UTF-8 boundary safety; tool_name is echoed in `CapEvent`.
- **Integration tests:** build a synthetic tool that returns > 25KB; run it through the `_get_tools` wrapping path with compaction **enabled**; assert the `ToolMessage` content is capped and the log line fired. Run the same synthetic tool with compaction **disabled** and assert the content is pass-through byte-identical.
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
