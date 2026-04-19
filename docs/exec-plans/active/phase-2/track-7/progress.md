# Phase 2 Track 7 — Context Window Management: Progress

| Task | Component | Status | Description |
|------|-----------|--------|-------------|
| Task 1 | Agent Config Extension | Not started | `agent_config.context_management` sub-object: Jackson, validation, canonicalisation |
| Task 2 | Compaction Constants + Thresholds | Not started | `compaction/defaults.py` + `compaction/thresholds.py` — platform constants + `resolve_thresholds()` |
| Task 3 | Per-Tool-Result Cap | Not started | `compaction/caps.py` head+tail byte cap; tool-wrapper integration; `compaction.per_result_capped` log |
| Task 4 | Tier 1 Transform | Not started | `clear_tool_results()` pure function with monotone `cleared_through_turn_index` |
| Task 5 | Tier 1.5 Transform | Not started | `truncate_tool_call_args()` pure function with monotone `truncated_args_through_turn_index` |
| Task 6 | Tier 3 Summarizer | Not started | `summarize_slice()` + retry + cost ledger (`compaction.tier3`) + Langfuse span |
| Task 7 | State + Pipeline + Graph Integration | Not started | `CompactionEnabledState`, `compact_for_llm()`, `agent_node` wiring, budget carve-out for `compaction.tier3` |
| Task 8 | Pre-Tier-3 Memory Flush | Not started | One-shot pre-Tier-3 `memory_note` nudge; heartbeat skip; `memory_flush_fired_this_task` one-shot |
| Task 9 | Dead-Letter Reason Enum | Not started | Migration + Java/Python plumbing for `context_exceeded_irrecoverable` |
| Task 10 | Console — Context Management Form | Not started | Agent edit form "Context management" section + Playwright scenario |
| Task 11 | Integration + Browser Tests | Not started | 15-AC E2E + cache-stability regression + parity tests + Playwright scenarios |

## Notes

- Canonical design contract: `docs/design-docs/phase-2/track-7-context-window-management.md`. Read before implementing any task.
- Default `enabled=true` on rollout; opt-out via `context_management.enabled=false` on the agent. No migration of existing rows is required (absent sub-object = platform defaults).
- Only DB schema change in this track: enum addition for `dead_letter_reason = context_exceeded_irrecoverable` (Task 9).
- Tasks 4 and 5 both add functions to `compaction/transforms.py`; Task 7 edits `executor/graph.py` heavily. Parallelise only with `isolation: "worktree"` per AGENTS.md §Parallel Subagent Safety.
- Pre-Tier-3 memory flush is gated on Track 5's `agent.memory.enabled` — Track 7 does not ship Track 5 tool registration; it reuses the existing `memory_note` surface.
- Track 7 integrates with Track 3's budget enforcement: `compaction.tier3` is a named-node carve-out alongside `memory_write` (skip per-task pause check, keep hourly-spend accounting).
- Regression gate in CI: every compaction unit test runs twice (enabled + disabled) to guard the "disabled = pre-Track-7 behavior" invariant.
- Thresholds are **fraction-only** in v1. An `aggressive_compaction=true` per-agent override is deliberately deferred — revisit only if post-rollout telemetry shows tier3_fire_rate above 1 per 100 calls.
