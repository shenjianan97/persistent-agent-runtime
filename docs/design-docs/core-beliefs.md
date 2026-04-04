# Core Beliefs — Key Architectural Invariants

These are the foundational architectural decisions that govern all phases. They are not phase-specific — they apply everywhere.

1. **Agent = data, not a process.** Agent config (persona, model, tools, memory, budget) lives in DB. Workers load agent config to "become" that agent. Agents never "go down."

2. **Task belongs to one agent** (immutable). Worker is the stateless process that executes tasks.

3. **Checkpoint-resume, not event-sourced replay.** LLM calls are non-deterministic — Temporal-style replay doesn't work.

4. **Database-as-queue (Phase 1).** PostgreSQL `FOR UPDATE SKIP LOCKED`. Eliminates dual-write problem.

5. **Strong consistency on the execution path.** Lease ownership plus database locks protect execution; the task `version` field is for lifecycle transitions and auditing, not per-checkpoint optimistic concurrency.

6. **Phase 1 recovery model is conservative.** Previously checkpointed nodes are not re-executed, but an interrupted in-flight node may be re-executed in full after crash recovery. Phase 1 enforces idempotent-only tools at submission time; non-idempotent tool guards are deferred to Phase 2.

7. **Tools via MCP protocol.** Phase 1 uses a co-located MCP server for built-in tools. Phase 2 introduces the Custom Tool Runtime (BYOT): customers upload MCP server containers, the platform runs them in isolated compute within the same VPC.

8. **Phase 1 scope excludes subgraphs and budget enforcement.** Phase 1 uses a single top-level LangGraph only; budget enforcement is deferred to Phase 2.

9. **LLMs are stateless.** Memory is simulated by assembling prompts from stored data (agent config + long-term memory from S3 + step history from PostgreSQL).

10. **Two-level memory:** step checkpoints in PostgreSQL double as conversation history within a task. Long-term memory is distilled knowledge across tasks, stored as append-only entries in S3 with compaction.
