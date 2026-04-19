<!-- AGENT_TASK_START: task-9-pre-tier3-memory-flush.md -->

# Task 9 — Pre-Tier-3 Memory Flush

## Agent Instructions

**CRITICAL PRE-WORK:**
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — section "Pre-Tier-3 memory flush" and Validation rule 7 ("Memory-disabled agents never fire the pre-Tier-3 flush").
2. `docs/design-docs/phase-2/track-5-memory.md` — `memory_note` tool shape and how it's registered per-task.
3. `services/worker-service/executor/compaction/pipeline.py` (from Task 8) — the pre-flush hook site.
4. `services/worker-service/executor/graph.py` — how `agent_node` handles follow-up state and heartbeat resume after checkpoint restore.
5. Task 8's `test_compaction_pipeline.py` — test patterns for pipeline units.

**CRITICAL POST-WORK:**
1. Run `make worker-test` and `make e2e-test`.
2. Update Task 9 status in `docs/exec-plans/active/phase-2/track-7/progress.md`.

## Context

Before Tier 3 summarization would otherwise fire for the first time in a task, the pipeline inserts one agentic turn asking the agent to call `memory_note` for anything worth persisting. This is the ONLY Track 5 ↔ Track 7 coupling. The flush:

- Fires at most once per task (`memory_flush_fired_this_task`).
- Fires only when `agent.memory.enabled AND context_management.pre_tier3_memory_flush`.
- Is skipped on heartbeat/recovery turns (detection: `len(raw_messages) <= state.last_super_step_message_count` — no new `ToolMessage` or `HumanMessage` since the last super-step; this is positional, NOT the "last two messages are both AIMessage" heuristic which misfires on rate-limit retries and pure-reasoning turns).
- Does not fire when `memory.enabled=false` even if `pre_tier3_memory_flush=true` in config.

Control flow when the flush fires:

1. Pipeline detects Tier 3 would fire AND pre-flush conditions hold.
2. Pipeline sets `state_updates["memory_flush_fired_this_task"] = True`.
3. Pipeline inserts the one-shot `SystemMessage` at the END of the compacted messages list (in-memory only, NOT returned in `state_updates["messages"]`) and **skips Tier 3 this call**.
4. The main agent LLM call proceeds on this turn; the agent may call `memory_note` (Track 5's tool).
5. On the next agent-node call, the flag is already True; Tier 3 proceeds normally if threshold still exceeded.

## Task-Specific Shared Contract

Function: extend `compact_for_llm` in Task 8's pipeline with a new helper `should_fire_pre_tier3_flush(state, agent_config, raw_messages) -> bool`:

- Returns `False` unless all of:
  - `context_management.pre_tier3_memory_flush=true`
  - `agent.memory.enabled=true` (read from `agent_config.memory.enabled`)
  - `state.memory_flush_fired_this_task` is False
  - Not a heartbeat turn (see detection below)
- When it returns True, the pipeline inserts the flush SystemMessage, skips Tier 3 on this call, and returns `state_updates = {"memory_flush_fired_this_task": True, ...watermarks from tier 1/1.5}`.

Heartbeat detection (positional, NOT message-pair-based):

```python
def _is_heartbeat_turn(
    raw_messages: list[BaseMessage],
    last_super_step_message_count: int,
) -> bool:
    """A heartbeat/recovery turn has no new ToolMessage or HumanMessage since
    the last agent super-step. Detect by comparing the current message-list
    length against the watermark persisted at the end of the previous
    super-step.
    """
    return len(raw_messages) <= last_super_step_message_count
```

This matches Design §Validation rule 10. The earlier "last two messages are both `AIMessage`" heuristic was wrong — it misfires on rate-limit retry loops (the previous call succeeded and wrote an AIMessage, the retry is legitimate new work) and on pure-reasoning turns (consecutive AIMessages can be valid). The positional comparison is unambiguous: new work = new message appended; no new work = no new message.

`last_super_step_message_count` is a new state field introduced in Task 8 (added to `RuntimeState` in Task 8). At the end of every agent super-step, the pipeline writes `last_super_step_message_count = len(raw_messages)` so the next call's heartbeat detection is accurate.

Flush system-message shape (exact string):

```
You are about to have older context summarized. This is your one chance in
this task to preserve cross-task-valuable facts. Call memory_note for anything
you want to remember in future tasks — decisions, user preferences, non-obvious
facts. If nothing qualifies, reply with an empty response.
```

Wrap in `SystemMessage(content=<above>, additional_kwargs={"compaction": True, "compaction_event": "pre_tier3_memory_flush"})` for Langfuse visibility.

## Affected Component

- **Service/Module:** Worker Service — Compaction pipeline
- **File paths:**
  - `services/worker-service/executor/compaction/pipeline.py` (modify — wire the flush hook that Task 8 left as a `pass`)
  - `services/worker-service/tests/test_compaction_pre_tier3_flush.py` (new)
- **Change type:** function addition + unit tests

## Dependencies

- **Must complete first:** Task 8 (owns `compact_for_llm` and the hook site).
- **Provides output to:** Task 12 (E2E tests exercise the full flush flow).
- **Cross-track dependency:** Track 5 `memory_note` tool must be registered on the agent when `memory.enabled=true`. Task 9 does NOT register tools; it assumes Track 5's per-task tool registration is already correct.

## Implementation Specification

In `pipeline.py`, replace the Task-7 placeholder:

```python
# Task 8 placeholder:
# if pre_flush_should_fire(...):
#     pass
```

with:

```python
if should_fire_pre_tier3_flush(state, agent_config, raw_messages):
    flush_message = SystemMessage(
        content=_PRE_TIER3_FLUSH_PROMPT,
        additional_kwargs={
            "compaction": True,
            "compaction_event": "pre_tier3_memory_flush",
        },
    )
    compacted_after_flush = [*compacted_messages, flush_message]
    return CompactionPassResult(
        messages=compacted_after_flush,
        state_updates={
            **watermark_updates_from_tier_1_and_1_5,
            "memory_flush_fired_this_task": True,
        },
        events=[*events, MemoryFlushEvent(fired_at_step=step_index)],
        tier3_skipped=False,  # flush is not a Tier 3 skip; Tier 3 simply didn't run
    )
```

And gate the subsequent Tier 3 branch behind `and not (flush just fired)`.

Define module-level constant `_PRE_TIER3_FLUSH_PROMPT` with the exact string above. Define `MemoryFlushEvent` as a small dataclass with a `log()` method emitting `compaction.memory_flush_fired`.

## Acceptance Criteria

- [ ] With `memory.enabled=true`, `pre_tier3_memory_flush=true`, `memory_flush_fired_this_task=False`, and Tier 3 would otherwise fire: the flush fires, state advances `memory_flush_fired_this_task=True`, Tier 3 is skipped THIS call, `compaction.memory_flush_fired` is emitted.
- [ ] On the NEXT call with the same threshold situation, the flush does NOT re-fire (flag is True); Tier 3 proceeds.
- [ ] With `memory.enabled=false`, the flush never fires regardless of `pre_tier3_memory_flush` value; Tier 3 proceeds normally if threshold met.
- [ ] With `pre_tier3_memory_flush=false`, the flush never fires; Tier 3 proceeds normally.
- [ ] Heartbeat detection (positional): when `len(raw_messages) <= state.last_super_step_message_count` (no new `ToolMessage` or `HumanMessage` since the last agent super-step), `_is_heartbeat_turn` returns True → flush is skipped even if all other conditions hold. `compaction.memory_flush_fired` is NOT emitted. This deliberately does NOT use the "last two messages are both AIMessage" heuristic, which misfires on rate-limit retries and pure-reasoning turns.
- [ ] The flush `SystemMessage` is appended at the END of the compacted messages list (so it's the most recent system-context before the agent acts).
- [ ] The flush message byte content exactly matches `_PRE_TIER3_FLUSH_PROMPT`.
- [ ] The flush is one-shot across the task — even on redrive / follow-up that resumes from a post-flush checkpoint, the flag remains True and the flush does NOT re-fire. Explicit test: construct a state with `memory_flush_fired_this_task=True`, trigger checkpoint save, trigger redrive from that checkpoint, verify the flag is restored to True and the flush does not re-insert.
- [ ] The flush SystemMessage is NOT persisted to graph state — it appears only in the compacted view for the one LLM call it was inserted for. Verified by: inspecting `state["messages"]` after `agent_node` returns and asserting no `SystemMessage` with `additional_kwargs["compaction_event"] == "pre_tier3_memory_flush"` is present.
- [ ] `make worker-test` — all unit tests pass.

## Testing Requirements

- **Unit tests (pipeline-level, mocked summarizer):** all gating paths — each condition flipped independently, confirm flush fires only in the correct combination.
- **Heartbeat detection (positional):** assert `_is_heartbeat_turn` returns True when `len(raw_messages) <= last_super_step_message_count` and False when new messages have been appended since the previous super-step. Include explicit test fixtures for: (a) rate-limit retry (consecutive AIMessages but new message count unchanged → True), (b) pure-reasoning turn followed by normal tool call (message count advanced → False), (c) redrive from mid-task checkpoint (count restored correctly → False when tool result lands).
- **Redrive-safety test:** construct a state with `memory_flush_fired_this_task=True` and assert flush does NOT re-fire on subsequent calls.
- **Integration test:** simulate a multi-turn task that crosses the Tier 3 threshold; assert the flush fires exactly once and `memory_note` is callable (tool is registered — this depends on Track 5 being live; if not, the integration test is flagged as requiring Track 5 and skipped in its absence).

## Constraints and Guardrails

- Do not register `memory_note` from Task 8 — Track 5 owns that. Task 9 only inserts the system message.
- Do not call `memory_note` directly from the pipeline — the agent decides whether to call it.
- Do not fire the flush on every Tier 3 firing; one-shot only.
- Do not reset `memory_flush_fired_this_task` within a task. (The flag naturally resets on a new task — new graph state.)
- Do not emit the flush event when the heartbeat skip fires.
- Do not add a second flush opportunity (e.g., "every 10 Tier 3 firings") — v1 is one-shot; revisit only if metrics show insufficient signal.
- Do not change the prompt wording without updating the Acceptance Criterion — the byte-exact string is the contract.

## Assumptions

- Track 5's `memory_note` tool is registered on the agent when `agent.memory.enabled=true`. If Track 5 has not landed yet, the pipeline still inserts the flush message; the agent simply won't have the tool and will respond without calling it. That is acceptable — Track 7 should not hard-depend on Track 5 running.
- `agent_config.memory.enabled` is reachable from the agent config Task 8 receives.
- The agent LLM call handles the added SystemMessage gracefully — verified by LangChain behavior.

<!-- AGENT_TASK_END: task-9-pre-tier3-memory-flush.md -->
