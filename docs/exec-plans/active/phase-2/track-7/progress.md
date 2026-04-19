# Phase 2 Track 7 — Context Window Management: Progress

| Task | Component | Status | Description |
|------|-----------|--------|-------------|
| Task 1 | Agent Config Extension | Done | `agent_config.context_management` sub-object (3 fields, no `enabled`): Jackson, validation, canonicalisation |
| Task 2 | State Schema Unification | Done | **Pure refactor.** Unified `RuntimeState` TypedDict in `executor/compaction/state.py`. `MemoryEnabledState` deleted. All existing Track 5 tests pass (561 passed). `grep MemoryEnabledState services/worker-service` → zero hits. |
| Task 3 | Compaction Constants + Thresholds | Done | `compaction/defaults.py` + `compaction/thresholds.py` — platform constants + `resolve_thresholds()`; `PLATFORM_EXCLUDE_TOOLS` includes memory_search + task_history_get |
| Task 4 | Per-Tool-Result Cap | Not started | `compaction/caps.py` head+tail byte cap; tool-wrapper integration |
| Task 5 | Tier 1 Transform | Not started | `clear_tool_results()` pure function with monotone `cleared_through_turn_index` |
| Task 6 | Tier 1.5 Transform | Not started | `truncate_tool_call_args()` pure function with monotone `truncated_args_through_turn_index` |
| Task 7 | Tier 3 Summarizer | Not started | `summarize_slice()` + retry + cost ledger (`compaction.tier3`) + Langfuse span |
| Task 8 | Pipeline + Graph Integration | Not started | Track 7 state fields added to `RuntimeState`; `compact_for_llm()`; `agent_node` wiring; budget carve-out for `compaction.tier3` |
| Task 9 | Pre-Tier-3 Memory Flush | Not started | One-shot pre-Tier-3 `memory_note` nudge; positional heartbeat detection; `memory_flush_fired_this_task` one-shot |
| Task 10 | Dead-Letter Reason Enum | Not started | Migration (CHECK-constraint DROP+ADD) + Java/Python plumbing for `context_exceeded_irrecoverable` |
| Task 11 | Console — Context Management Form | Done | Agent edit form "Context management" section (3 fields, no `enabled` toggle); `ContextManagementSection.tsx` + 22 unit tests; `AgentConfig` type extended; Scenario 15 added to `CONSOLE_BROWSER_TESTING.md` |
| Task 12 | Integration + Browser Tests | Not started | 14-AC E2E + cache-stability regression + Playwright scenarios |

## Notes

- Canonical design contract: `docs/design-docs/phase-2/track-7-context-window-management.md`. Read before implementing any task.
- **Track 7 is always-on for all agents.** No per-agent `enabled` toggle and no runtime opt-out — context management is platform infrastructure. Incident response is the standard deploy-rollback path, the same as Tracks 3/4/5.
- **Task 2 is a hard blocker for every other worker-side task.** It is a pure refactor (unified `RuntimeState`) with zero behavior change. All existing Track 5 tests pass before Tasks 3–8 begin (Task 9 serialises after Task 8). This isolates refactor failures from feature failures.
- **State schema is append-only.** LangGraph has no schema-migration API ([langgraphjs #536](https://github.com/langchain-ai/langgraphjs/issues/536)); we add fields, never remove or rename. Regression test in Task 2 loads a pre-refactor checkpoint fixture and asserts clean resume.
- **Reducer-safe defaults.** Every list-reducer field defaults to `[]`, every dict to `{}`, every string to `""`, every int to `0`, every bool to `False`. Never `None` — `operator.add` crashes on None, and any non-instantiable type (unions, `Optional[T]`, etc.) leaves the LangGraph channel MISSING so the reducer is bypassed on the seed write (see closed-as-by-design [langgraph #4305](https://github.com/langchain-ai/langgraph/issues/4305)).
- Only DB schema change in this track: enum addition for `dead_letter_reason = context_exceeded_irrecoverable` (Task 10).
- Tasks 5 and 6 both add functions to `compaction/transforms.py`; Task 8 edits `executor/graph.py` heavily. Parallelise only with `isolation: "worktree"` per AGENTS.md §Parallel Subagent Safety.
- `compaction/__init__.py` is owned by Task 8 — Tasks 2–7 leave it as docstring-only to avoid parallel-merge conflicts.
- Pre-Tier-3 memory flush is gated on Track 5's `agent.memory.enabled` — Track 7 does not ship Track 5 tool registration; it reuses the existing `memory_note` surface.
- Track 7 integrates with Track 3's budget enforcement: `compaction.tier3` is a named-node carve-out alongside `memory_write`.
- Thresholds are **fraction-only** in v1. An `aggressive_compaction=true` per-agent override is deliberately deferred — revisit only if post-rollout telemetry shows tier3_fire_rate above 1 per 100 calls.
