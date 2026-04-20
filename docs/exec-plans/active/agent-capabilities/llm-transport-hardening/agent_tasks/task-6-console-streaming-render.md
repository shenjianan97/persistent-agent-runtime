<!-- AGENT_TASK_START: task-6-console-streaming-render.md -->

# Task 6 — Console: Render Streaming Progress Entries

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/exec-plans/active/agent-capabilities/llm-transport-hardening/plan.md` — overall architecture.
2. `docs/CONSOLE_TASK_CHECKLIST.md` — **mandatory merge gate** for any Console task. Read every section.
3. `docs/CONSOLE_BROWSER_TESTING.md` — scenario authoring rules + selection matrix. This task adds a new scenario.
4. `services/console/src/features/tasks/...` — locate the conversation-log rendering surface (search for `ConversationLog`, `useTaskEvents`, or `kind === "llm_response"`).
5. Track 7 Task 13 spec — establishes how existing kinds render, sets the visual pattern this task should match.

**This task SHIPS code + a CONSOLE_BROWSER_TESTING scenario only. The orchestrator runs Playwright after merge.** Per `feedback_playwright_enforcement.md`, browser verification is the orchestrator's job — do not call `make start` / `make stop` or any Playwright MCP tool from inside this task.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make console-test` (or the project's narrowest equivalent that covers the touched component).
2. Update `progress.md` row 6 to "Done — awaiting orchestrator browser verification".

## Context

Task 5 emits two new conversation-log kinds: `llm_stream_progress` and `llm_stream_complete`. They are visible to the Console via the existing SSE stream — but with no render branch they appear as raw JSON or are silently filtered out.

UX goals:

- During a stream, the page shows a **single rolling line** with the latest progress (`generating · 23 s · 1.8k chars · 12 chunks`). Not one timeline entry per progress tick.
- On `llm_stream_complete`, the rolling line collapses into the LLM-response card that follows (so the timeline doesn't accumulate scaffolding entries from completed calls).
- On stream failure (no `llm_stream_complete`), the rolling line is replaced by the existing error-display card.

## Task-Specific Shared Contract

- **No new SSE plumbing.** The conversation-log SSE that Console already consumes carries the new kinds.
- **`data-testid` attributes** for the new render branches:
  - Rolling progress line: `data-testid="llm-stream-progress"` (re-used across ticks).
  - Inside it: `data-testid="llm-stream-progress-elapsed"`, `data-testid="llm-stream-progress-chunks"`, `data-testid="llm-stream-progress-chars"`.
- **Collapse behavior:** when the next `llm_stream_complete` (or `llm_response`) entry arrives, the rolling line is removed from the timeline. The completed LLM-response card remains.
- **Auto-scroll behavior:** existing auto-scroll-to-tail is preserved. Progress updates do **not** push the auto-scroll cursor (otherwise users reading earlier history get jumped to the bottom every 10 s).
- **Backward compat:** if the SSE delivers an unknown kind, the existing fallback (silently skip or render JSON) continues to apply — these kinds are additive.

## Affected Component

- **Service/Module:** Console — Tasks
- **File paths:**
  - `services/console/src/features/tasks/...` (modify the conversation-log render component — locate via grep)
  - `services/console/src/features/tasks/__tests__/...` (extend tests for the new render branch)
  - `docs/CONSOLE_BROWSER_TESTING.md` (add a new scenario per the authoring rules)
- **Change type:** modification + new test + new browser-testing scenario

## Dependencies

- **Must complete first:** Task 5 (the kinds being rendered must exist).
- **Provides output to:** Task 9 (repro test asserts the Console shows progress).
- **Shared interfaces/contracts:** the `data-testid` selectors are the contract Playwright scenarios use.

## Implementation Specification

### Render branch

In the conversation-log render component:

- Detect entries with `kind === "llm_stream_progress"`. Maintain a single "active stream progress" element keyed by checkpoint_id. Update its content as new progress entries for the same checkpoint arrive.
- Detect entries with `kind === "llm_stream_complete"`. Remove the active stream progress element for that checkpoint.
- Render text: `generating · {elapsed_s.toFixed(0)} s · {chars_text + chars_tool_call} chars · {chunks} chunks`. Use the model name as a tooltip.

### Tests (unit / component)

- Render with one `llm_stream_progress` entry — expect a `llm-stream-progress` element with the correct text.
- Receive a second progress entry for the same checkpoint — expect the existing element to update, NOT a second element to appear.
- Receive a `llm_stream_complete` for that checkpoint — expect the progress element to be removed.
- Receive a progress entry for checkpoint A and a different checkpoint B — expect two distinct progress elements.

### Browser-testing scenario

Add to `docs/CONSOLE_BROWSER_TESTING.md` a scenario per the existing Template structure:

- **Scenario name:** "Long-running LLM call shows live streaming progress."
- **Preconditions:** an agent configured with a slow model (or a stub) that takes ≥ 30 s for one LLM call.
- **Steps:** create a task → open task page → assert `llm-stream-progress` element appears → assert text updates at least once → assert element disappears when LLM call completes → assert the LLM-response card is present.
- **Expected:** no page reload, no errors in the browser console.

## Acceptance Criteria

- [ ] Console renders `llm_stream_progress` as a single rolling line per checkpoint, not one entry per tick.
- [ ] Component tests cover all four render scenarios above.
- [ ] New scenario added to `docs/CONSOLE_BROWSER_TESTING.md` per the project's authoring rules.
- [ ] `make console-test` passes.
- [ ] `data-testid` attributes match the contract above so Playwright scenarios can target them.

## Out of Scope

- Running Playwright (orchestrator's job).
- Adding any new SSE topic or backend route.
- Visual polish beyond the rolling-line pattern (e.g., progress bar, percentage estimate) — out of scope for this hardening cycle.

<!-- AGENT_TASK_END -->
