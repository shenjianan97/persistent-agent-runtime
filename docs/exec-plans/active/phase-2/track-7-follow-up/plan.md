# Phase 2 Track 7 Follow-up — Replace-and-Rehydrate Context Management: Orchestrator Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Track 7's strict-append, in-place "mutate `state.messages` with placeholders and append to `summary_marker`" compaction with a clean **replace-and-rehydrate** architecture built on LangGraph's `pre_model_hook`. The durable journal (`state["messages"]`) is kept nearly append-only and large payloads are offloaded at ingestion; the agent-visible context window is a lightweight projection assembled fresh on every turn from three regions (`summary`, `middle`, `keep window`). When the projection approaches the model's context budget we *replace* the summary rather than append to it. This closes the three production-observed issues on Track 7 (poor summary quality, irrecoverable tool results, uncaught provider-contract regressions) and simplifies the invariants: one summary string, one monotone watermark, one S3-backed recall path.

**Architecture:** Three distinct entities, each with a single well-defined job.

1. **`state["messages"]` (durable journal).** Persisted by the LangGraph checkpointer into `agent_runtime_checkpoints`. Nearly append-only — the only sanctioned mutation is the Option-C "reference-after-summarization" replacement that swaps a recalled ToolMessage's content for an S3 reference once its range has been absorbed into `summary`. Large payloads never enter `state["messages"]` inline: Tier 0 ingestion offload uploads content >20 KB to S3 (`platform-artifacts` bucket) and stores a reference + preview instead. This applies to both `ToolMessage.content` and large string keys inside `AIMessage.tool_calls[*].args` (`content`, `new_string`, `text`, `body`).
2. **`pre_model_hook` (lightweight curator).** Runs before every agent LLM invocation. Assembles `summary + middle + keep_window` from the journal, estimates tokens, and — if the projection exceeds ~85 % of the agent's context window — fires the summarizer to produce a *new* summary over `prior_summary + middle`. The new summary replaces the old one; `summarized_through` advances to the start of the keep window; `middle` becomes empty. No placeholders are written back to `state["messages"]`.
3. **Agent LLM context window (ephemeral).** The projection returned by `pre_model_hook`. Recomputed every turn; never checkpointed as a distinct entity.

The `recall_tool_result` built-in tool lets the agent pull full content back from S3 by `tool_call_id`. Its output bypasses Tier 0's ingestion trim (the agent explicitly asked for full content). Projection rules for recalled ToolMessages: inside the keep window → shown in full; outside the keep window → dropped from view to prevent a re-offload / re-recall oscillation. When a compaction step would absorb a recalled ToolMessage's range into `summary`, the pipeline performs the one sanctioned mutation — Option C: replace the ToolMessage's content in `state["messages"]` with a reference (lossless: S3 still holds the bytes).

**Tech Stack:** Python + LangGraph (`pre_model_hook` primitive) + LangChain (worker); existing `S3Client` on `platform-artifacts`; pytest (offline suite); GitHub Actions (scheduled offline eval). No Java / API contract changes beyond an additive config flag; no DB migrations.

---

## A1. Implementation Overview

Seven tasks. Canonical landing order for the core pipeline rewrite is **2 → 4 → 3 → 5**. Tasks 1, 6, 7 are independent and can land at any time.

1. **Summarizer prompt hardening + output cap + truncation telemetry.** Rewrite the summarizer prompt so the word/token target is binding, add `max_tokens=SUMMARIZER_MAX_OUTPUT_TOKENS` (default `1500`) as a safety net, and emit `compaction.tier3_output_truncated` WARN when the cap fires. Drop the now-obsolete `compaction.summary_marker_oversized` WARN — there is no cumulative marker to overflow because Task 3 replaces instead of appends. No other behavior change.

2. **Recursive chunking for oversized `middle`.** When the slice fed to the summarizer (`prior_summary + middle`) exceeds `summarizer_context_window - SUMMARIZER_INPUT_HEADROOM_TOKENS` (default headroom `12_000`), split **`middle` only** into halves, summarize each chunk, then summarize the concatenation. `prior_summary` is carried only at the top-level call — it is never chunked and never recursively re-summarized. Prerequisite for 1M-window agents where even a single `middle` region can overflow a 200K summarizer.

3. **Replace compaction pipeline with `pre_model_hook` + replace-and-rehydrate.** The load-bearing rewrite. Wire a `pre_model_hook` into `agent_node`'s LangGraph definition that:
   - Reads `state["messages"]`, `state["summary"]`, `state["summarized_through"]`.
   - Computes `keep_window_start` positionally: walk back from the end past the `KEEP_TOOL_USES`-th-most-recent `ToolMessage` (default `3`), then align to the preceding `AIMessage` with matching `tool_calls` (orphan-prevention, same rule shipped in PR #80). The keep window is a **positional slice**, not a type filter — any `HumanMessage` or text-only `AIMessage` interleaved within that range is included verbatim.
   - Assembles the projection `[SystemMessage(system_prompt), SystemMessage(summary)?, *messages[summarized_through:keep_window_start], *messages[keep_window_start:]]` — the user's system prompt is always the head; the summary `SystemMessage` is present only when `state["summary"]` is non-empty.
   - Estimates projection tokens and, if ≥ `COMPACTION_TRIGGER_FRACTION × context_window` (default `0.85`, matching DeepAgents; raised from Track 7's 0.75), invokes `summarize_slice(prior_summary, middle)` → new summary string → *replaces* `state["summary"]`, advances `state["summarized_through"]` to `keep_window_start`, increments `state["tier3_firings_count"]`. The middle region is now empty and the projection is re-assembled for this turn's LLM call.
   - Returns the projection as the `pre_model_hook` output; LangGraph passes it to the ChatModel call without mutating `state["messages"]`.
   - State-shape changes: **DROP** `summary_marker`, `cleared_through_turn_index`, `truncated_args_through_turn_index`. **ADD** `summary: str` (single replaceable field, `last` reducer), `summarized_through: int` (monotone `max` reducer), `tier3_firings_count: int` (monotone `max` reducer), `memory_flush_fired_this_task: bool` (monotone `or` reducer).
   - Old `state["messages"]` entries written by Track 7 — strings with "[tool output not retained …]" placeholders — are tolerated on load; the new pipeline simply projects them verbatim (they are already short, so Tier 0 ignores them). No historical rewrite.

4. **Tier 0 ingestion offload — tool results AND tool-call args.** Extend the existing tool-execution wrapper in `executor/graph.py` to offload both (a) `ToolMessage.content` and (b) large string keys (`content`, `new_string`, `text`, `body`) inside `AIMessage.tool_calls[*].args` to S3 when their byte length exceeds `OFFLOAD_THRESHOLD_BYTES` (default `20_000`). Below threshold, store inline. Above threshold, upload full content to `platform-artifacts` under a deterministic key scheme and store a reference + preview in `state["messages"]`. This supersedes Track 7's "head+tail 25KB trim" and fully absorbs the roles of the now-dropped Tier 1 (tool-result clearing) and Tier 1.5 (arg truncation) — Task 3's projection rules handle the "old tool results are noise" problem positionally, and the ingestion offload bounds the byte footprint of every journal entry regardless of projection.

5. **`recall_tool_result` tool + config flag + system-prompt hint + Option-C reference-replacement.** Platform built-in LangChain tool. Takes a `tool_call_id`, fetches the full content from S3, and returns it as a `ToolMessage`. Validates task-scope (reject cross-task / cross-tenant). Registered automatically when `context_management.offload_tool_results` is `true` (default `true`, new additive config flag; worker+API only in v1, not Console-editable). A short directive is appended to the agent's system prompt explaining the tool. Behaviour rules:
   - Recalled `ToolMessage` carries metadata `{"recalled": True, "original_tool_call_id": "..."}`.
   - **Recall output bypasses Tier 0 ingestion trim** — the agent explicitly asked for full content.
   - Projection rules in `pre_model_hook`: recalled ToolMessage inside the current keep window → shown in full; outside the keep window → **dropped from view** (prevents a re-offload / re-recall loop).
   - **Option C — reference-after-summarization.** When the compaction step in Task 3 advances `summarized_through` past a recalled ToolMessage's index, the pipeline replaces that message's `content` in `state["messages"]` with a reference string (`"[tool output offloaded — fetch with recall_tool_result('...')]"`). Lossless — the full bytes remain in S3. This is the **one acceptable mutation** to `state["messages"]`; documented and covered by a dedicated unit test.

6. **Offline real-provider evaluation suite.** `services/worker-service/tests_offline/` with ~5 curated long-running agent scenarios (tool-use chain forcing Tier 3, MCP integration, memory flush, dead-letter recovery, recall path). Real providers, not mocks. Scheduled via `.github/workflows/offline-llm-eval.yml` — nightly + pre-release + `workflow_dispatch`. **Never on per-commit CI.** Per-run hard budget cap using the existing cost-ledger sum; exceeding cap produces a clean skip + workflow annotation, not a hang. Cross-run / cumulative daily budget state is deferred — v1 uses per-run cap only.

7. **Complete `CONTEXT_WINDOW_DEFAULTS` coverage.** Extend `services/model-discovery/main.py`'s explicit context-window map to cover the remaining active-provider model families: Gemini (1.5 / 2.x), Mistral (Large-3, Devstral-2, Magistral, Ministral, Voxtral), Nvidia (Nemotron Nano / Super), Qwen (Qwen3-32B / Coder / VL / Next), Moonshot Kimi K2 / K2.5, Writer Palmyra. Each value verified against provider docs. Preserves the "any entry below fallback ⇒ deny-listed" invariant.

**Canonical prior contract:** `docs/design-docs/phase-2/track-7-context-window-management.md` — the original Track 7 design. This follow-up supersedes the "three-tier in-place transform" section; the other sections (thresholds, config surface, observability model, dead-letter reason) continue to apply.

---

## A2. Impacted Components

| Component | Path | Change Type | Description |
|-----------|------|-------------|-------------|
| Summarizer prompt + caps | `services/worker-service/executor/compaction/summarizer.py` | modification | Tightened prompt; `max_tokens=SUMMARIZER_MAX_OUTPUT_TOKENS`; emit `compaction.tier3_output_truncated` WARN on truncation; drop `summary_marker_oversized` WARN path |
| Recursive chunking | `services/worker-service/executor/compaction/summarizer.py` (new helper) | new code | `_chunk_summarize_middle(middle_messages, ...)` splits `middle` only; `prior_summary` carried at top level; invoked when `estimate_tokens(prior_summary + middle) > summarizer_ctx - SUMMARIZER_INPUT_HEADROOM_TOKENS` |
| `pre_model_hook` + projection | `services/worker-service/executor/compaction/pre_model_hook.py` (new) | new code | Builds `summary + middle + keep_window` projection; positional keep-window computation with orphan alignment; invokes summarizer when over threshold; writes back `summary`, `summarized_through`, `tier3_firings_count` |
| State schema (replace shape) | `services/worker-service/executor/compaction/state.py` | modification | **Drop** `summary_marker`, `cleared_through_turn_index`, `truncated_args_through_turn_index`. **Add** `summary: str` (last reducer), `summarized_through: int` (max), `tier3_firings_count: int` (max), `memory_flush_fired_this_task: bool` (or). Tolerate legacy fields on load — ignore going forward |
| Pipeline deletion / replacement | `services/worker-service/executor/compaction/pipeline.py`, `transforms.py` | deletion / rewrite | Delete `clear_tool_results`, `truncate_tool_call_args` and their call sites. `compact_for_llm` rewritten or removed in favour of `pre_model_hook`. `agent_node` in `executor/graph.py` switches to the `pre_model_hook` parameter on the ChatModel node |
| Ingestion offload | `services/worker-service/executor/graph.py` (tool wrapper), `services/worker-service/executor/compaction/ingestion.py` (new) | modification + new code | At tool-result construction time and at AIMessage tool-call construction time, route large string payloads (`>= OFFLOAD_THRESHOLD_BYTES`) through `offload_to_s3` and store reference + preview inline. Covers `ToolMessage.content` and `AIMessage.tool_calls[*].args.{content,new_string,text,body}` |
| S3 offload store | `services/worker-service/executor/compaction/tool_result_store.py` (new) | new code | `ToolResultArtifactStore` abstract + `S3ToolResultStore` (backed by existing `S3Client` on `platform-artifacts`) + `InMemoryToolResultStore` (tests); deterministic key scheme keyed on `(tenant_id, task_id, tool_call_id)` |
| Recall tool | `services/worker-service/executor/builtin_tools/recall_tool_result.py` (new) | new code | LangChain tool; validates task-scope; sets `recalled=True` + `original_tool_call_id`; auto-registered when `offload_tool_results=true` |
| Recall projection rules | `services/worker-service/executor/compaction/pre_model_hook.py` | new code (same file) | Inside keep window → full; outside → drop from view; Option-C replacement performed when `summarized_through` advances past a recalled ToolMessage's index |
| Agent config flag | `services/api-service/.../model/request/ContextManagementConfigRequest.java`, `services/worker-service/executor/compaction/defaults.py` | modification | Additive `offload_tool_results: bool = true`. Jackson mapping + validation + canonicalisation. Not surfaced in Console in v1 |
| System-prompt hint | `services/worker-service/executor/graph.py` | modification | When `offload_tool_results=true`, append short directive describing `recall_tool_result` to the agent's system prompt |
| Offline suite | `services/worker-service/tests_offline/` (new dir), `.github/workflows/offline-llm-eval.yml` (new) | new code + workflow | ~5 scenarios; nightly + pre-release + dispatch; per-run hard budget cap via cost-ledger sum; clean-skip + annotation on cap exceed |
| Context-window coverage | `services/model-discovery/main.py`, `services/model-discovery/tests/test_discover_models.py` | modification | Explicit entries for Gemini / Mistral / Nvidia / Qwen / Moonshot / Writer families; consistency-invariant test extended |
| Tests | `services/worker-service/tests/test_compaction_summarizer_caps.py`, `tests/test_compaction_chunking.py`, `tests/test_pre_model_hook_projection.py`, `tests/test_ingestion_offload.py`, `tests/test_recall_tool_result.py`, `tests/test_option_c_reference_replacement.py` | new | Unit per task + projection integration + Option-C invariant |

---

## A3. Dependency Graph

**Canonical landing order (single source of truth; referenced by every task file):**

```
 Task 1  ─────────────────────────────────── (independent; can land anytime)
 Task 6  ─────────────────────────────────── (independent; runs on schedule)
 Task 7  ─────────────────────────────────── (independent; can land anytime)

 Task 2  (recursive chunking — safety for large middle regions)
    │
    ▼
 Task 4  (Tier 0 ingestion offload for results AND tool-call args)
    │
    ▼
 Task 3  (pre_model_hook + replace-and-rehydrate pipeline rewrite)
    │
    ▼
 Task 5  (recall_tool_result + config flag + system-prompt + Option-C)
```

The order **2 → 4 → 3 → 5** is load-bearing:

- **Task 2 before Task 3** because the new pipeline feeds the summarizer `prior_summary + middle`, and `middle` on a 1M-window agent can exceed any 200K summarizer. Chunking must exist first or Task 3 is unsafe in production.
- **Task 4 before Task 3** because the projection rules assume every journal entry is already byte-bounded; without ingestion offload the keep-window region can still blow past the context budget in a single super-step.
- **Task 5 after Task 3** because recall-tool projection rules and Option-C reference-replacement are defined in terms of the new pipeline's watermark (`summarized_through`) and projection assembly.

**Parallelisation opportunities:**

- **Tasks 1, 6, 7** are fully independent — parallelise freely with the core chain.
- **Tasks 1 and 2** both touch `summarizer.py` — serialise or use `isolation: "worktree"` per AGENTS.md §Parallel Subagent Safety.
- **`pre_model_hook.py` is primarily touched by Task 3 and extended by Task 5.** The 3 → 5 serial order keeps them conflict-free. Any parallelisation attempt must use worktrees.
- Task 4 touches `executor/graph.py`; so does Task 3 (for the `pre_model_hook` wiring). Land Task 4 first, then Task 3 — otherwise use worktrees.

---

## A4. Deployment / Rollout

- **No database migrations.** All changes are code + config.
- **No API contract breakage.** The `context_management.offload_tool_results` flag is additive with default `true`. Not Console-editable in v1 (worker + API only).
- **Legacy state fields are tolerated on load.** Checkpoints written by Track 7 may still contain `summary_marker`, `cleared_through_turn_index`, `truncated_args_through_turn_index`. The new state schema ignores them on deserialize and does not write them going forward. The new `summary` field stays empty on legacy checkpoints until the first post-deploy compaction fires — at which point it is populated fresh from the (still-intact) `state["messages"]`.
- **One-time KV-cache miss on first post-deploy summarization for in-flight tasks.** Task 3 replaces the summary rather than appending, so the byte content of the prefix shifts on the first firing. Subsequent calls re-warm normally. Documented and accepted — this is strictly a per-task one-time cost.
- **Behavioural-correctness observation window.** Langfuse dashboards watched for: `tier3_fire_rate` (should decrease or hold — threshold raised to 0.85), `cache_hit_rate_ratio` (transient dip expected on day of deploy, back to baseline within 24h as in-flight tasks drain), `compaction.tier3_output_truncated` WARN count (should be rare; repeated firings on the same task indicate the prompt still isn't binding).
- **Rollback path:** revert the deploy. No schema to unwind. Legacy Track 7 code and legacy state fields are forward-compatible with the reverted worker.
- **Offline suite (Task 6) runs on its own schedule** and is not a deploy gate. First run can happen post-deploy.

---

## A5. Follow-ups Explicitly Deferred

- **Research subagent pattern.** Context isolation via disposable sub-scopes for tool-use bursts — complementary to this track's durable-journal approach, substantially bigger design change. Tracked as [#84](https://github.com/shenjianan97/persistent-agent-runtime/issues/84) for a future track.
- **Tier 1 / Tier 1.5 Console debug visibility.** The Track 7 tiers they referred to no longer exist after this track. An operator-debug view over `pre_model_hook` decisions (which messages were kept / dropped / recalled) may be valuable; out of scope for v1. Worker structured-log events (`compaction.tier3_fired`, `compaction.offload_emitted`, `compaction.option_c_replacement`) remain the operator surface.
- **Prompt caching.** Tracked separately as [#52](https://github.com/shenjianan97/persistent-agent-runtime/issues/52). Interacts with the replace-style summary (replacement invalidates the cached prefix on each compaction), so implementation will need to coordinate cache-breakpoint placement with the new pipeline.
- **Proactive pre-execution offload for predictably large tools.** Today offload fires at ingestion after the tool has returned. A proactive variant ("offload any result from tool X") is an optimisation for known-huge tools (e.g. large file reads). Defer until recall-tool telemetry shows it's worth the added complexity.
- **Cross-run / cumulative daily budget state for the offline suite.** v1 uses per-run cap only; cumulative budgeting is a follow-up if the nightly cost creeps up.
- **`context_management.offload_tool_results` Console-editable in v1** — worker + API only. Surface it in the Agent edit form once the flag has seen production usage and the invariants stabilise.

---

## References

- **Parent design doc:** `docs/design-docs/phase-2/track-7-context-window-management.md` — this follow-up supersedes its three-tier-transform section; other sections continue to apply.
- **Production incident reference:** task `1717c12b-aee3-4632-b29e-b3d0e269e87f` — surfaced every gap this track closes; triaged against the shipped Track 7 pipeline.
- **Parent Track 7 ship:** PR #80, squash-merged as commit `9454390` on 2026-04-19.
- **Related GH issues:** [#81](https://github.com/shenjianan97/persistent-agent-runtime/issues/81) (offline eval suite — Task 6), [#82](https://github.com/shenjianan97/persistent-agent-runtime/issues/82) (Tier 3 summary quality — Tasks 1–3), [#83](https://github.com/shenjianan97/persistent-agent-runtime/issues/83) (tool-result offload + recall — Tasks 4–5).
- **Deferred:** [#84](https://github.com/shenjianan97/persistent-agent-runtime/issues/84) (research subagent pattern), [#52](https://github.com/shenjianan97/persistent-agent-runtime/issues/52) (prompt caching).
- **Upstream references:**
  - LangGraph `pre_model_hook` — https://langchain-ai.github.io/langgraph/ (React-style agent with pre-model hook for context trimming).
  - DeepAgents context engineering — https://github.com/langchain-ai/deepagents (summarization middleware fires at ~85 % of context window; replace-style summary).
- **Defaults (module constants in `compaction/defaults.py`, easy to tune):**
  - `OFFLOAD_THRESHOLD_BYTES = 20_000`
  - `KEEP_TOOL_USES = 3`
  - `COMPACTION_TRIGGER_FRACTION = 0.85`
  - `SUMMARIZER_MAX_OUTPUT_TOKENS = 1500`
  - `SUMMARIZER_INPUT_HEADROOM_TOKENS = 12_000`
