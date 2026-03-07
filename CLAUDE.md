# CLAUDE.md — Project Context

## Project

Cloud-Native Persistent Agent Runtime — a cloud-native durable execution runtime for AI agents. Solves three problems existing workflow engines (Temporal, Restate) don't handle: non-deterministic LLM execution, unbounded agent memory growth, and cost-aware scheduling. Initial deployment targets AWS managed services, but core architecture is cloud-agnostic.

## Key Architecture Decisions

- **Agent = data, not a process.** Agent config (persona, model, tools, memory, budget) lives in DB. Workers load agent config to "become" that agent. Agents never "go down."
- **Task belongs to one agent** (immutable). Worker is the stateless process that executes tasks.
- **Checkpoint-resume, not event-sourced replay.** LLM calls are non-deterministic — Temporal-style replay doesn't work.
- **Database-as-queue (Phase 1).** PostgreSQL `FOR UPDATE SKIP LOCKED`. Eliminates dual-write problem.
- **Strong consistency on the execution path.** Lease ownership plus database locks protect execution; the task `version` field is for lifecycle transitions and auditing, not per-checkpoint optimistic concurrency.
- **Phase 1 recovery model is conservative.** Previously checkpointed nodes are not re-executed, but an interrupted in-flight node may be re-executed in full after crash recovery. Phase 1 enforces idempotent-only tools at submission time (all pre-registered tools served via co-located MCP server are read-only); non-idempotent tool guards are deferred to Phase 2.
- **Tools via MCP protocol.** Phase 1 uses a co-located MCP server for built-in tools. Phase 2 introduces the Custom Tool Runtime (BYOT): customers upload MCP server containers, the platform runs them in isolated compute within the same VPC. Replaces the former BYOW (Bring Your Own Worker) concept — customers provide tools, not workers.
- **Phase 1 scope excludes subgraphs and budget enforcement.** Phase 1 uses a single top-level LangGraph only; budget enforcement is deferred to Phase 2.
- **LLMs are stateless.** Memory is simulated by assembling prompts from stored data (agent config + long-term memory from S3 + step history from PostgreSQL).
- **Two-level memory:** step checkpoints in PostgreSQL double as conversation history within a task. Long-term memory is distilled knowledge across tasks, stored as append-only entries in S3 with compaction.

## Tech Stack

- Java (core runtime), Python (workers), TypeScript (CDK)
- PostgreSQL (Aurora Serverless v2) — Phase 1 state store + queue
- SQS FIFO — Phase 2 queue (transactional outbox)
- ECS Fargate — workers
- OpenTelemetry → CloudWatch — observability
- Bedrock + OpenAI/Anthropic APIs — LLM integration

## Documents

| File | Purpose |
|------|---------|
| PROJECT.md | High-level project overview: vision, user stories, phases, tradeoffs, tech stack |
| design/PHASE1_DURABLE_EXECUTION.md | Phase 1 design: architectural context, entity model, API contract, DB schema, sequence diagrams, lease protocol, idempotency, observability |
| design/PHASE2_MULTI_AGENT.md | Phase 2 design: Agent entity, cost-aware scheduling, memory compaction, Custom Tool Runtime / BYOT (placeholder) |
| design/DESIGN_NOTES_PHASE2.md | Phase 2+ reference material: full Agent entity, long-term memory model, scaling analysis, DynamoDB design |

## Project Stages

### Stage 1 — Problem & Scope [DONE]
- PROJECT.md — vision, differentiation, user stories, phases
- Core concepts and tradeoff positions documented

### Stage 2 — Technical Design [IN PROGRESS]
- PHASE1_DURABLE_EXECUTION.md — Phase 1 architectural context, entity model, API contract, DB schema, sequence diagrams (done)
- DESIGN_NOTES_PHASE2.md — Phase 2+ reference material extracted from former DESIGN.md (done)
- PHASE2_MULTI_AGENT.md — Phase 2 scope placeholder (done)
- Review and refine Phase 1 design before implementation (not started)

### Stage 3 — Implementation Plan [NOT STARTED]
- Task breakdown with dependencies and ordering
- Milestones — what's demoable at each checkpoint
- Test strategy — integration tests for crash recovery, idempotency

### Stage 4 — Implementation [NOT STARTED]
- Write code iteratively against the API contract and DB schema

### Stage 5 — Validation [NOT STARTED]
- End-to-end crash-recovery demo
- Performance testing against scaling numbers
- Record demo video

### Stage 6 — Launch / Publish [NOT STARTED]
- README with setup instructions
- Blog post about one hard problem solved
- Push to GitHub
