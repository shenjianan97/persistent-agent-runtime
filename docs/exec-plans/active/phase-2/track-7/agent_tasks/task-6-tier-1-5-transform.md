<!-- AGENT_TASK_START: task-6-tier-1-5-transform.md -->

# Task 6 — Tier 1.5 Transform: Tool-Call Argument Truncation

## Agent Instructions

**CRITICAL PRE-WORK:**
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — section "Tier 1.5: tool-call argument truncation".
2. `services/worker-service/executor/compaction/defaults.py` — `KEEP_TOOL_USES`, `TRUNCATABLE_TOOL_ARG_KEYS`, `ARG_TRUNCATION_CAP_BYTES`.
3. `services/worker-service/executor/compaction/transforms.py` (from Task 5) — pattern for monotone pure transforms.
4. LangChain `AIMessage.tool_calls` shape — how `tool_calls` list is structured (each entry has `id`, `name`, `args` dict).

**CRITICAL POST-WORK:**
1. Run `make worker-test`. Every new unit test must pass.
2. Update Task 6 status in `docs/exec-plans/active/phase-2/track-7/progress.md`.

## Context

Tier 1.5 targets the largest token offender in production workloads: `AIMessage.tool_calls[*].args.content` for `sandbox_write_file`, `new_string` for `sandbox_edit` (when Track 8 lands), etc. Once the agent has called `sandbox_write_file(path="foo.py", content=<5KB of code>)`, that 5KB of content is pure dead weight in every subsequent LLM call — the agent doesn't need to re-read its own input.

Tier 1.5 rewrites `args[key]` in older `AIMessage.tool_calls` to `[{orig_bytes} bytes — arg truncated after step {i}]` when the key is in `TRUNCATABLE_TOOL_ARG_KEYS` and the value is a string longer than `ARG_TRUNCATION_CAP_BYTES`.

Monotonicity is the same as Tier 1: `truncated_args_through_turn_index` only advances.

## Task-Specific Shared Contract

Function signature:

```python
def truncate_tool_call_args(
    messages: list[BaseMessage],
    truncated_args_through_turn_index: int,
    keep: int,
    truncatable_keys: frozenset[str],
    cap_bytes: int,
) -> TruncateResult
```

`TruncateResult`:

```python
@dataclass(frozen=True)
class TruncateResult:
    messages: list[BaseMessage]
    new_truncated_args_through_turn_index: int
    args_truncated: int
    bytes_saved: int
```

Semantics:

- Input `messages` immutable; construct new list.
- Only `AIMessage` with non-empty `tool_calls` are candidates. Other message types pass through.
- Protection window: the most recent `keep` tool-invocation turns — derived the same way as Tier 1, by counting `ToolMessage` positions. An `AIMessage.tool_calls` older than `protect_from_index` is candidate for arg truncation.
- For each candidate `AIMessage`, walk every `tool_call` in `tool_calls`. For each `(key, val)` in `call.args`:
  - Skip if `key not in truncatable_keys`.
  - Skip if `not isinstance(val, str)`.
  - Skip if `len(val.encode("utf-8")) <= cap_bytes`.
  - Skip if val already looks truncated (`val.startswith("[") and " bytes — arg truncated after step" in val`).
  - Replace with `f"[{len(val)} bytes — arg truncated after step {i}]"` where `i` is the `AIMessage`'s index in `messages`.
- Rebuild the `AIMessage` via its copy semantics — do NOT mutate the original. LangChain's `model_copy`/`copy(update=...)` (depending on version) returns a new instance with updated `tool_calls`.
- Monotonicity: `new_truncated_args_through_turn_index = max(truncated_args_through_turn_index, protect_from_index)`.
- If no work done, return original `messages` list verbatim.

## Affected Component

- **Service/Module:** Worker Service — Compaction transforms
- **File paths:**
  - `services/worker-service/executor/compaction/transforms.py` (modify — add `truncate_tool_call_args` alongside Task 5's `clear_tool_results`)
  - **Do NOT edit `compaction/__init__.py`** — Task 8 owns its final shape. Import `truncate_tool_call_args`, `TruncateResult` directly from `executor.compaction.transforms`.
  - `services/worker-service/tests/test_compaction_transforms_truncate.py` (new)
- **Change type:** function addition + unit tests

## Dependencies

- **Must complete first:** Task 3 (imports constants).
- **Parallel-safe with:** Task 5 writes to the same file. Use `isolation: "worktree"` if parallelising.
- **Provides output to:** Task 8 (pipeline invokes `truncate_tool_call_args` as the Tier 1.5 step).

## Implementation Specification

Pseudocode:

```python
def truncate_tool_call_args(messages, truncated_args_through_turn_index, keep, truncatable_keys, cap_bytes):
    tool_msg_positions = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    if len(tool_msg_positions) <= keep:
        return TruncateResult(messages, truncated_args_through_turn_index, 0, 0)

    protect_from_index = tool_msg_positions[-keep]
    new_watermark = max(truncated_args_through_turn_index, protect_from_index)
    if new_watermark == truncated_args_through_turn_index:
        return TruncateResult(messages, truncated_args_through_turn_index, 0, 0)

    compacted = list(messages)
    args_truncated = 0
    bytes_saved = 0
    for i, m in enumerate(messages):
        if i >= new_watermark:
            continue
        if not isinstance(m, AIMessage) or not m.tool_calls:
            continue
        new_tool_calls = []
        touched = False
        for call in m.tool_calls:
            new_args = dict(call["args"] if isinstance(call, dict) else call.args)
            for key in list(new_args.keys()):
                val = new_args[key]
                if key not in truncatable_keys:
                    continue
                if not isinstance(val, str):
                    continue
                val_bytes = len(val.encode("utf-8"))
                if val_bytes <= cap_bytes:
                    continue
                if _already_truncated(val):
                    continue
                placeholder = f"[{val_bytes} bytes — arg truncated after step {i}]"
                new_args[key] = placeholder
                args_truncated += 1
                bytes_saved += (val_bytes - len(placeholder.encode("utf-8")))
                touched = True
            new_call = _rebuild_tool_call(call, new_args)
            new_tool_calls.append(new_call)
        if touched:
            compacted[i] = _rebuild_ai_message(m, new_tool_calls)

    return TruncateResult(
        messages=compacted,
        new_truncated_args_through_turn_index=new_watermark,
        args_truncated=args_truncated,
        bytes_saved=bytes_saved,
    )


def _already_truncated(val: str) -> bool:
    return val.startswith("[") and " bytes — arg truncated after step" in val
```

`_rebuild_tool_call` and `_rebuild_ai_message` handle LangChain's version-specific shapes for tool_calls (`ToolCall` TypedDict vs `dict`-typed). Match the pattern used in the existing `graph.py` `agent_node` handling. In practice LangChain 0.2+ represents `tool_calls` as a list of dicts with keys `id`, `name`, `args` (and optionally `type`); assignment of `args` in a new dict and `m.model_copy(update={"tool_calls": new_tool_calls})` is the cleanest form.

## Acceptance Criteria

- [ ] Given an `AIMessage` with `tool_calls=[{"id": "c1", "name": "sandbox_write_file", "args": {"path": "foo.py", "content": "x"*5000}}]` outside the protection window, the `content` arg is rewritten to `"[5000 bytes — arg truncated after step K]"`.
- [ ] `args["path"]` (not a truncatable key) is unchanged.
- [ ] A short `content` arg (≤ `ARG_TRUNCATION_CAP_BYTES`) is unchanged.
- [ ] Non-string args (numbers, booleans, dicts, lists) are unchanged even when the key is truncatable.
- [ ] An arg whose value already looks truncated is NOT re-truncated (idempotency).
- [ ] Watermark is monotone — two calls, second using first's output + watermark, produce byte-identical messages and no further work.
- [ ] Determinism: two calls with the same inputs return byte-identical `messages` lists.
- [ ] `AIMessage`s inside the protection window are untouched.
- [ ] Messages other than `AIMessage` are untouched.
- [ ] `make worker-test` — unit tests pass.

## Testing Requirements

- Mirror Task 5's test structure: table-driven coverage of cap-threshold, idempotency, determinism, non-AIMessage pass-through, non-string args, protection-window boundary.
- Cache-stability regression: run twice, assert byte-identical outputs.
- Cross-version LangChain compatibility: if `tool_calls` is a list of dicts, and if it's a list of `ToolCall` typed dicts — both representations must work. Add a test that injects each shape.

## Constraints and Guardrails

- Do NOT mutate the input `AIMessage` or its `tool_calls` list. Always construct new objects.
- Do NOT introduce a new LangChain dependency. Use whatever version is pinned in `pyproject.toml`.
- Do NOT truncate non-string args. Track 7 v1 scope is string args only.
- Do NOT read any env var, DB, or network. Pure transform.
- Do NOT combine Task 5 and Task 6 transforms — two orthogonal functions.

## Assumptions

- LangChain version in use supports both `m.model_copy(update={...})` and direct `AIMessage(content=..., tool_calls=...)` construction. If neither works cleanly in the version pinned, use `m.copy(update={...})` or fall back to `AIMessage(**{**m.dict(), "tool_calls": new_tool_calls})`.
- The worker ships Python 3.11+.

<!-- AGENT_TASK_END: task-6-tier-1-5-transform.md -->
