<!-- AGENT_TASK_START: task-1-summarizer-prompt-and-caps.md -->

# Task 1 — Summarizer Prompt Hardening, `max_tokens` Safety Net, and Summary-Size Telemetry

## Agent Instructions

**CRITICAL PRE-WORK:**
1. Read `docs/design-docs/phase-2/track-7-context-window-management.md` — section "Tier 3: retrospective summarization" (existing design). Note: under the Option-3 (replace-and-rehydrate) pivot, the summary is replaced each firing — the prior strict-append `summary_marker` invariant no longer applies.
2. Read `services/worker-service/executor/compaction/summarizer.py` in full. Note the current prompt at `SUMMARIZER_PROMPT` (top of the file), the `llm.ainvoke` call site, and the `SummarizeResult` dataclass.
3. Read GH issue [#82](https://github.com/shenjianan97/persistent-agent-runtime/issues/82) — production evidence motivating the prompt / cap / telemetry hardening.
4. Read `services/worker-service/tests/test_compaction_summarizer.py` for existing test patterns. New tests for this task go in a new file (see §Acceptance Criteria).

**CRITICAL POST-WORK:**
1. Run `make worker-test`. All existing tests must stay green.
2. Update `docs/exec-plans/active/phase-2/track-7-follow-up/progress.md` to mark Task 1 Done with a one-line delivery note.

---

## Context

Production task `1717c12b-aee3-4632-b29e-b3d0e269e87f` triggered retrospective summarization with a 39-turn slice. The slice was 4,974 tokens of input, but the summarizer returned **3,628 tokens** — ~7× the ~500-token target the prompt requests. Under Option 3 the summary is replaced each firing (not appended), so cumulative growth is not the concern it once was; however, a runaway single summary still consumes a large fraction of the keep-window budget and pushes compaction cadence higher than necessary.

Root cause: the current prompt treats the 400-word limit as advisory, no `max_tokens` is passed to the LLM call, and there is no telemetry on truncation events.

## Goal

Make summarizer output size bounded in practice:

- Prompt that a compliant model treats as binding.
- Hard output cap as a safety net when compliance fails.
- Telemetry on truncation events so regressions surface in observability instead of production cost.

## Contract — Behaviour Changes

### 1. Summarizer prompt rewrite

- Rewrite `SUMMARIZER_PROMPT` so the token budget is binding, not advisory. The exact wording is the implementer's choice, but it MUST:
  - State the budget in tokens (≤500 tokens), not words — tokens are the load-bearing unit.
  - State that responses over the budget will be truncated and the tail lost.
  - Instruct the model to preserve the most recent facts when forced to choose.
  - Include one short concrete example showing "overly long summary → truncated with tail dropped".
- No change to the list of preservation priorities (files, URLs, decisions, errors, identifiers).

### 2. `max_tokens=1500` on the summarizer LLM call

- Pass `max_tokens=1500` to `llm.ainvoke` in `summarize_slice`. Choice of 1500 (not 500) gives the model headroom to gracefully wrap up in a well-behaved case while hard-capping pathological runaways at ~3× the target.
- The exact value MUST be a module-level constant so Task 6's offline suite can reference it.

### 3. `compaction.tier3_output_truncated` WARN event

- When the summarizer response's finish reason indicates truncation (provider-specific: `"length"` on OpenAI/Bedrock Converse, `"max_tokens"` on Anthropic), emit a `compaction.tier3_output_truncated` structured log at WARN level with `tenant_id`, `agent_id`, `task_id`, `tokens_out` fields.
- The truncated summary is still consumed (it replaces the prior summary under Option 3's replace-and-rehydrate model). The log is the signal; behaviour stays correct under truncation.

## Affected Files

- `services/worker-service/executor/compaction/summarizer.py` — prompt constant, `max_tokens` constant + pass, truncation-finish-reason detection, WARN emission.
- `services/worker-service/executor/compaction/defaults.py` — new constant: `SUMMARIZER_MAX_OUTPUT_TOKENS = 1500`.
- `services/worker-service/tests/test_compaction_summarizer_caps.py` — **new file** with tests per §Acceptance Criteria.

## Dependencies

None. Task 1 can ship independently.

## Out of Scope for This Task

- Recursive chunking when the **input** slice (the "middle" region) is oversized — that's Task 2.
- The `pre_model_hook` + replace-and-rehydrate rewrite itself — that's Task 3.

## Interaction with Task 3

Task 3 is the `pre_model_hook` + replace-and-rehydrate rewrite. Under Option 3 the summarizer sees `prior_summary + middle` (raw messages, not Tier-1-stubbed), which is 10–50× larger than today's Tier-1-stubbed input. Legitimately longer summaries are expected. The `max_tokens=1500` safety net is sized for *today's* workload; after Task 3 lands it may need to rise. Task 3's acceptance criteria include a truncation-WARN-rate bound (≤5% of firings) that triggers a re-calibration of `SUMMARIZER_MAX_OUTPUT_TOKENS` upward if exceeded. This task's cap of 1500 is the starting point, not a permanent value.

## Acceptance Criteria (observable behaviours)

1. A mock summarizer LLM configured to return 3,000 tokens has its output truncated at 1,500 tokens by `max_tokens`. The resulting `SummarizeResult.summary_text` is ≤ 1,500 tokens.
2. Following the previous case, a WARN log event named `compaction.tier3_output_truncated` is emitted exactly once per truncated firing, including the task context fields.
3. Existing `test_compaction_summarizer.py` passes unchanged (no behaviour regression for non-truncated paths).

## Pattern references in existing code

- Structured-log event shape: `services/worker-service/executor/compaction/pipeline.py` event-emitter pattern (`_compaction_logger.info` / `.warning` call sites).
- Finish-reason extraction from provider metadata: `_extract_tokens` in `summarizer.py` already walks `response_metadata` / `usage_metadata`. Pattern for finish-reason is analogous: check `response_metadata.get("finish_reason")` / `.get("stop_reason")` across provider shapes.
- WARN level for operator-actionable events: `services/worker-service/executor/graph.py:_get_model_context_window` (WARN on fallback — upgraded in PR #80's hardening commit).

<!-- AGENT_TASK_END -->
