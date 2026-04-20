<!-- AGENT_TASK_START: task-9-repro-test-and-cleanup.md -->

# Task 9 — Repro Test + `TEMP_DEBUG_BEDROCK` Cleanup

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/exec-plans/active/agent-capabilities/llm-transport-hardening/plan.md` — overall architecture.
2. Issue [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85) — acceptance criteria are mirrored in this task's tests.
3. The current state of `services/worker-service/executor/graph.py` lines 1170–1230 to confirm the `TEMP_DEBUG_BEDROCK` markers are present and bound the right region for removal.
4. `services/worker-service/tests/test_long_output_no_timeout.py` doesn't exist yet — this task creates it.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make test` for the worker (full unit-test suite). Confirm green.
2. Manually drive the original repro task (`Help me research on all features of aws bedrock…`) on the same agent + GLM-5 and confirm it either completes successfully or fails fast with `llm.max_tokens_reached`. Capture the worker log snippet in the PR description.
3. Update `progress.md` row 9 to "Done".
4. Move the active plan directory to `docs/exec-plans/completed/agent-capabilities/llm-transport-hardening/` once the orchestrator has merged this task and closed #85.

## Context

This task is the merge gate for #85. It verifies that the combined effect of Tasks 1–8 produces the behavior the issue's acceptance criteria require, and removes the investigation-only `TEMP_DEBUG_BEDROCK` markers that were added during root-cause analysis.

## Task-Specific Shared Contract

- The repro test exercises the full streaming + maxTokens + timeout path. It uses a stub LLM (no real Bedrock call from CI) that mimics the observed GLM-5 pattern: yields chunks slowly with a multi-thousand-token tool-use payload.
- After this task merges, **no `TEMP_DEBUG_BEDROCK` references remain** in the worker source tree. The structured `llm_stream_progress` / `llm_stream_complete` events from Task 5 replace them.

## Affected Component

- **Service/Module:** Worker — Executor (test + cleanup)
- **File paths:**
  - `services/worker-service/tests/test_long_output_no_timeout.py` (new)
  - `services/worker-service/executor/graph.py` (modify — strip `TEMP_DEBUG_BEDROCK` blocks added during investigation)
- **Change type:** new test + targeted removals

## Dependencies

- **Must complete first:** Tasks 1, 2 + 3, 4, 5, 6, 7, 8 — all merged.
- **Provides output to:** closes #85.
- **Shared interfaces/contracts:** none.

## Implementation Specification

### New test: `tests/test_long_output_no_timeout.py`

Three integration-shaped tests using a stub LLM:

1. **`test_streaming_long_output_completes_without_timeout`**
   - Stub yields 200 `AIMessageChunk` objects over 60 seconds wall time (use `asyncio.sleep` between chunks).
   - Final merged `AIMessage` has 5000 chars of text and a tool_use block.
   - Configure transport with `read_timeout_s=30` (smaller than total wall time but larger than any inter-chunk gap).
   - Assert the call completes successfully — proves per-chunk read timeout, not whole-call timeout, is what governs.
   - Assert at least 5 `llm_stream_progress` entries appear in the conversation log.

2. **`test_max_tokens_reached_surfaces_warning_not_timeout`**
   - Stub yields chunks ending with `response_metadata={"stopReason": "max_tokens"}`.
   - Assert worker log contains a `llm.max_tokens_reached` WARN with the configured cap.
   - Assert no `ReadTimeoutError` is raised; the message flows downstream as a normal `AIMessage` (the agent_node gets to react to the truncation on the next turn).

3. **`test_no_legacy_timeout_kwarg_warning_at_startup`**
   - Construct a Bedrock `ChatBedrockConverse` via `providers.create_llm`.
   - Capture warnings (e.g., via `warnings.catch_warnings`).
   - Assert no warning matches `r".*was transferred to model_kwargs.*"`.

### Cleanup: `executor/graph.py`

Remove every block bounded by `# TEMP_DEBUG_BEDROCK` comments. The blocks live around lines 1170–1230 (range may have shifted post-Tasks 4/5; locate via `grep -n TEMP_DEBUG_BEDROCK services/worker-service/executor/graph.py`).

After cleanup, run `grep -rn TEMP_DEBUG_BEDROCK services/` and confirm zero matches outside this task spec.

## Acceptance Criteria

- [ ] All three tests in `test_long_output_no_timeout.py` pass.
- [ ] No `TEMP_DEBUG_BEDROCK` markers remain in `services/worker-service/`.
- [ ] Manual repro confirms the original failing prompt either completes or surfaces `llm.max_tokens_reached` (no silent timeout). Worker log evidence captured in the PR.
- [ ] All existing tests still pass.
- [ ] Issue #85 is closed by the merging PR.

## Out of Scope

- Real-Bedrock E2E runs in CI (gated behind a separate offline-real-provider suite — see issue #81).
- Console browser test (covered by Task 6 + the orchestrator's Playwright pass).
- Removing `/tmp/bedrock_dumps/`, `/tmp/bedrock_probe*.py`, `/tmp/replay_bedrock.py` (these are local-only artifacts; clean up in the orchestrator's session).

<!-- AGENT_TASK_END -->
