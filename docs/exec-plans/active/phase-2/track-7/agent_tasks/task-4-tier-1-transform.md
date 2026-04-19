<!-- AGENT_TASK_START: task-4-tier-1-transform.md -->

# Task 4 — Tier 1 Transform: Tool-Result Clearing

## Agent Instructions

**CRITICAL PRE-WORK:**
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — sections "Core design rules" (rules 1, 2, 3) and "Tier 1: tool-result clearing".
2. `services/worker-service/executor/compaction/defaults.py` (from Task 2) — `KEEP_TOOL_USES`, `PLATFORM_EXCLUDE_TOOLS`.
3. LangChain / LangGraph message types — `ToolMessage`, `AIMessage`, `SystemMessage`, `HumanMessage` — and how `tool_call_id` / `.name` are set on `ToolMessage`.
4. Track 5 `clear_tool_results`-adjacent code (if any) — prior-art for message-list transforms in this worker.

**CRITICAL POST-WORK:**
1. Run `make worker-test`. Every new unit test must pass.
2. Update `docs/exec-plans/active/phase-2/track-7/progress.md` to "Done" for Task 4.

## Context

Tier 1 is observation masking: replace older `ToolMessage.content` with a deterministic placeholder so the `tool_use` record remains (the agent still knows it called the tool) but the bulky result is gone.

Protection window: the most recent `KEEP_TOOL_USES` tool invocations retain their raw content. Everything older is candidate for clearing, minus the `exclude_tools_effective` set (platform + agent union).

Monotonicity: `cleared_through_turn_index` only advances. A message cleared at call N stays cleared (with the same placeholder string) at calls N+1, N+2, … — this is what preserves KV-cache.

## Task-Specific Shared Contract

Function signature:

```python
def clear_tool_results(
    messages: list[BaseMessage],
    cleared_through_turn_index: int,
    keep: int,
    exclude_tools_effective: frozenset[str],
) -> ClearResult
```

Where `ClearResult`:

```python
@dataclass(frozen=True)
class ClearResult:
    messages: list[BaseMessage]                 # Compacted view
    new_cleared_through_turn_index: int         # Watermark after this pass (>= input)
    messages_cleared: int                        # How many ToolMessages were actually rewritten this pass
    est_tokens_saved: int                        # Rough estimate of tokens saved (len diff / 3.5)
```

Semantics:

- Input `messages` is treated as immutable. The function constructs a **new** list; never mutates in place.
- Only `ToolMessage` instances are candidates. `AIMessage`, `HumanMessage`, `SystemMessage` are passed through unchanged.
- A `ToolMessage` is cleared if and only if:
  - Its index in `messages` is **strictly less than** the protection boundary (`protect_from_index`), AND
  - Its tool name is not in `exclude_tools_effective`, AND
  - It hasn't already been replaced with the platform placeholder (detect via a `[tool output not retained` prefix check to preserve idempotency — a second call on an already-cleared message returns the same message object).
- `protect_from_index` is derived from the positions of the `KEEP_TOOL_USES`-most-recent `ToolMessage` instances in `messages`. If there are ≤ `keep` tool messages total, `protect_from_index = 0` (nothing to clear).
- Placeholder shape:
  `[tool output not retained — {tool_name} returned {orig_bytes} bytes at step {index}]`
  Use `tool_message.name` for `tool_name` when present; else derive from the preceding `AIMessage.tool_calls` by matching `tool_call_id`. Use `len(tool_message.content.encode("utf-8"))` for `orig_bytes`, computed on the content at the time of clearing.
- Monotonicity: `new_cleared_through_turn_index = max(cleared_through_turn_index, protect_from_index)` — NEVER below input watermark.
- If `new_cleared_through_turn_index == cleared_through_turn_index`, no work was done; return the original messages list verbatim (not a copy) and `messages_cleared=0`, `est_tokens_saved=0`.
- Must be deterministic: running twice on the same input returns byte-identical output. Any `datetime.now()`-style nondeterminism is forbidden.

## Affected Component

- **Service/Module:** Worker Service — Compaction transforms
- **File paths:**
  - `services/worker-service/executor/compaction/transforms.py` (new — Task 4 adds `clear_tool_results`; Task 5 adds `truncate_tool_call_args` alongside)
  - **Do NOT edit `compaction/__init__.py`** — Task 7 owns its final shape. Import `clear_tool_results`, `ClearResult` directly from `executor.compaction.transforms`.
  - `services/worker-service/tests/test_compaction_transforms_clear.py` (new)
- **Change type:** new module + new function + unit tests

## Dependencies

- **Must complete first:** Task 2 (imports `KEEP_TOOL_USES`, `PLATFORM_EXCLUDE_TOOLS`).
- **Parallel-safe with:** Task 5 writes to the same file (`transforms.py`). Use `isolation: "worktree"` if parallelising — per AGENTS.md §Parallel Subagent Safety.
- **Provides output to:** Task 7 (pipeline invokes `clear_tool_results` as the Tier 1 step).

## Implementation Specification

Implement `clear_tool_results` per the contract above. Use `langchain_core.messages.ToolMessage` for the type check. Key pseudocode:

```python
def clear_tool_results(messages, cleared_through_turn_index, keep, exclude_tools_effective):
    tool_msg_positions = [
        i for i, m in enumerate(messages) if isinstance(m, ToolMessage)
    ]
    if len(tool_msg_positions) <= keep:
        return ClearResult(messages, cleared_through_turn_index, 0, 0)

    protect_from_index = tool_msg_positions[-keep]
    new_watermark = max(cleared_through_turn_index, protect_from_index)
    if new_watermark == cleared_through_turn_index:
        return ClearResult(messages, cleared_through_turn_index, 0, 0)

    # Build tool_call_id → tool_name map from preceding AIMessages so we can
    # recover the tool name even if ToolMessage.name is None. Note:
    # LangChain 0.2+ represents tool_calls as a list of dicts with string
    # keys (id, name, args, type) — not attribute access. Task 5 uses the
    # same shape; keep them consistent.
    tool_name_by_call_id = {}
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            for call in m.tool_calls:
                # Support both dict shape (LangChain >= 0.2) and the
                # occasional TypedDict variant. Never attribute access.
                call_id = call["id"] if isinstance(call, dict) else getattr(call, "id", None)
                call_name = call["name"] if isinstance(call, dict) else getattr(call, "name", None)
                if call_id is not None:
                    tool_name_by_call_id[call_id] = call_name

    compacted = list(messages)
    cleared_count = 0
    tokens_saved_est = 0
    for i, m in enumerate(messages):
        if i >= new_watermark:
            continue
        if not isinstance(m, ToolMessage):
            continue
        tool_name = m.name or tool_name_by_call_id.get(m.tool_call_id, "unknown_tool")
        if tool_name in exclude_tools_effective:
            continue
        if _already_cleared(m.content):
            continue
        orig_bytes = len(m.content.encode("utf-8"))
        placeholder = (
            f"[tool output not retained — {tool_name} returned "
            f"{orig_bytes} bytes at step {i}]"
        )
        compacted[i] = ToolMessage(
            content=placeholder,
            tool_call_id=m.tool_call_id,
            name=m.name,
        )
        cleared_count += 1
        tokens_saved_est += max(0, (orig_bytes - len(placeholder)) // 4)

    return ClearResult(
        messages=compacted,
        new_cleared_through_turn_index=new_watermark,
        messages_cleared=cleared_count,
        est_tokens_saved=tokens_saved_est,
    )


def _already_cleared(content: str) -> bool:
    return content.startswith("[tool output not retained —")
```

## Acceptance Criteria

- [ ] Given 5 `ToolMessage`s and `keep=3`, `clear_tool_results` clears the 2 oldest and leaves the 3 most recent intact.
- [ ] Given 3 `ToolMessage`s and `keep=3`, nothing is cleared; watermark unchanged; same list returned.
- [ ] `exclude_tools_effective = {"memory_note"}` causes every `memory_note` `ToolMessage` to retain its content regardless of age.
- [ ] A `ToolMessage` whose content already starts with `"[tool output not retained —"` is NOT re-cleared (idempotency).
- [ ] The watermark is monotone: calling `clear_tool_results` twice on the same state with the second input using the first output's watermark produces byte-identical messages on both passes and the watermark does not advance on the second pass.
- [ ] Determinism: two calls with the same inputs return byte-identical `messages` lists (assert `a == b`).
- [ ] A `ToolMessage` whose `.name` is `None` has the tool name recovered from the preceding `AIMessage.tool_calls[*].id == tool_call_id`.
- [ ] A `ToolMessage` whose tool name cannot be recovered falls back to `"unknown_tool"` in the placeholder.
- [ ] Placeholder string byte length is less than any realistic capped tool result (< 200 bytes).
- [ ] No message type other than `ToolMessage` is ever modified.
- [ ] `make worker-test` — all unit tests pass.

## Testing Requirements

- **Unit tests:** table-driven coverage of protection-window boundary, watermark monotonicity, exclude list, idempotency, determinism, tool-name recovery, non-tool-message pass-through.
- **Cache-stability regression:** run the function twice and assert the output lists are `==` AND the placeholder strings are byte-identical (this is the contract downstream relies on).
- **Watermark-advance-only test:** feed a watermark 10 turns past the protection boundary; assert the function does NOT regress the watermark and returns a no-op (`messages_cleared=0`).

## Constraints and Guardrails

- Do not mutate input messages. Construct a new list.
- Do not call `datetime.now()`, `uuid.uuid4()`, or any other non-deterministic function in the placeholder — all placeholder bytes must be derivable from `(tool_name, orig_bytes, index)`.
- Do not emit log lines from this function — logging is the pipeline orchestrator's job (Task 7).
- Do not read the summarizer model, provider credentials, or anything from the worker runtime — this is a pure transform.
- Do not combine with Task 5's `truncate_tool_call_args` into one function — they are independent.

## Assumptions

- LangChain `ToolMessage` is importable as `from langchain_core.messages import ToolMessage`. If the worker uses a different path, match the existing import style in `graph.py`.
- Tool name recovery via `AIMessage.tool_calls[*]` relies on LangChain's `ToolCall` shape (`id` + `name`). Verify against the version pinned in `pyproject.toml`.
- The worker ships Python 3.11+.

<!-- AGENT_TASK_END: task-4-tier-1-transform.md -->
