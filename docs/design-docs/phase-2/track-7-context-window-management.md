# Track 7 Design — Context Window Management

**Status: Stub — not yet designed. Requires its own brainstorm and design pass before planning.**

## Why this track exists

Long-running tasks accumulate tool-call arguments and tool-result content in the message history that LangGraph replays to the LLM on every step. Input tokens grow monotonically with the number of tool invocations. In one observed production task, input reached ~27,000 tokens with ~84,000 characters of tool content in history. The main offenders were `sandbox_write_file` arguments and `sandbox_read_file` results embedded in past `AIMessage` / `ToolMessage` records.

Without a compaction layer, tasks with more than a small number of tool invocations hit one of three failure modes:

1. provider context-window limits
2. rate-limit / TPM ceilings
3. cost-per-call that makes the task economically non-viable

This track closes that gap by adding a tiered compaction layer that runs inside the LangGraph executor loop, transforming the message list just before each LLM call. No schema changes, no new services, no new APIs — this is a worker-local transform.

## Relationship to Track 5 (Memory)

Track 5 (Memory) is a cross-task store; Track 7 is a within-task transform. They share zero schema, zero API, and zero UI. They do not block each other. If Track 7 ships first, the final memory-write node in Track 5 operates on a compacted history, which is cheaper to summarize. If Track 5 ships first, memory already works correctly — long tasks are simply more likely to fail before reaching the memory-write node.

Track 7 is recommended ahead of Track 5 on the grounds that memory is less useful if long-running tasks cannot complete.

## Shape of the proposed work (from GitHub issue #50)

**Three-tier compaction, modeled on Claude Code's approach:**

1. **Tier 1: Tool-result clearing (zero cost).** Before each LLM call, replace older `ToolMessage` content with placeholders such as `[Cleared — read 3,252 bytes from report.md]`. Keep the last N tool results intact as a protection window.
2. **Tier 1.5: Tool-call argument truncation (zero cost).** Truncate large `tool_calls` arguments in older `AIMessage` records — especially `sandbox_write_file.content`. Replace with `[Wrote 9,463 bytes to foo.py]`. This is typically the biggest win by token share.
3. **Tier 3: Retrospective LLM summarization (expensive, last resort).** When Tiers 1 and 1.5 are not sufficient, fire a cheap-model summarization pass that replaces prior messages with a structured summary. Only trigger near the context-window ceiling.

**Open design questions (to be answered during the brainstorm):**

- Global thresholds vs per-agent thresholds vs per-task thresholds
- Default protection window size (how many recent tool results are untouched)
- Which tool names get arg truncation by default
- Opt-out mechanism for workloads that genuinely need raw history
- Interaction with LangGraph checkpoints — do transforms run before or after checkpoint write?
- How to surface compaction events in traces / Langfuse
- Metrics for monitoring compaction effectiveness over time
- **Pre-compaction memory flush (Track 5 interaction).** Tier 3 summarization irreversibly drops message detail. To avoid losing salient context that the agent had not yet captured, Track 7 is expected to insert a short agentic turn immediately before Tier 3 runs, instructing the agent to call [`memory_note`](./track-5-memory.md#memory_notetext-string) for anything worth persisting. Open questions: when the flush fires (only before Tier 3, or also before aggressive Tier 1.5?), how it is throttled (once per task? once per N compactions?), whether it is opt-out per agent, and how to avoid firing on heartbeat / background turns. Track 5 exposes the `memory_note` primitive already; Track 7 owns the trigger design.

## References

- [GitHub issue #50](https://github.com/shenjianan97/persistent-agent-runtime/issues/50) — original proposal, failure modes observed in production
- [Claude Cookbook: Context Engineering](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools)
- [Manus: Context Engineering for AI Agents](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- [JetBrains: Observation masking research](https://blog.jetbrains.com/research/2025/12/efficient-context-management/)
- [LangMem: Summarization](https://langchain-ai.github.io/langmem/guides/summarization/)
- `anthropics/claude-code#27293` — upstream discussion of tool_use input bloat

## Next step

Run a dedicated brainstorm for this track (superpowers:brainstorming) before writing any implementation plan. This stub exists only to register the track's scope and block accidental coupling with Track 5.
