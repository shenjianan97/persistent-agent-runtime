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

- Java (core runtime), Python (workers), TypeScript (CDK, Console)
- React 19 + Vite + Tailwind/shadcn/ui — Console frontend
- PostgreSQL (Aurora Serverless v2) — Phase 1 state store + queue
- SQS FIFO — Phase 2 queue (transactional outbox)
- ECS Fargate — workers
- OpenTelemetry → CloudWatch — observability
- Bedrock + OpenAI/Anthropic APIs — LLM integration

## Documents

| File | Purpose |
|------|---------|
| docs/PROJECT.md | High-level project overview: vision, user stories, phases, tradeoffs, tech stack |
| docs/design/PHASE1_DURABLE_EXECUTION.md | Phase 1 design: architectural context, entity model, API contract, DB schema, sequence diagrams, lease protocol, idempotency, observability |
| docs/design/PHASE2_MULTI_AGENT.md | Phase 2 design: Agent entity, cost-aware scheduling, long-term memory, task event history, secret management hardening, Custom Tool Runtime / BYOT |
| docs/design/DESIGN_NOTES_PHASE3_PLUS.md | Phase 3+ reference material: scaling analysis, queue/storage evolution options, DynamoDB design, future tool integration |
| docs/implementation_plan/phase-1/plan.md | Phase 1 Orchestrator Plan detailing dependencies, AWS integration, and execution breakdown |
| docs/implementation_plan/phase-1/progress.md | Live tracking board for agent execution of Phase 1 |
| docs/implementation_plan/phase-1/agent_tasks/*.md | 8 parallelizable, single-responsibility execution templates for agents |
| experiments/langgraph/plan.md | Proof of concept strategy to validate LangGraph checkpointer exceptions |

## Project Stages

### Stage 1 — Problem & Scope [DONE]
- docs/PROJECT.md — vision, differentiation, user stories, phases
- Core concepts and tradeoff positions documented

### Stage 2 — Technical Design [DONE]
- PHASE1_DURABLE_EXECUTION.md — Phase 1 architectural context, entity model, API contract, DB schema, sequence diagrams (done)
- PHASE2_MULTI_AGENT.md — consolidated Phase 2 design doc (done)
- DESIGN_NOTES_PHASE3_PLUS.md — Phase 3+ reference material extracted from former Phase 2+ notes (done)
- Review and refine Phase 1 design before implementation (done)

### Stage 3 — Implementation Plan [DONE]
- Hand-off: Translate Phase 1 design into `docs/implementation_plan/phase-1/plan.md` Orchestrator Plan
- Splitting Prompts: Generate and split explicit constraints into 8 parallelizable agent task spec files (`task-1` through `task-8`) 
- Tracking: Created `docs/implementation_plan/phase-1/progress.md` for orchestrator execution tracking
- POC logic: Created LangGraph POC validation tasks in `experiments/langgraph/` to test assumptions

### Stage 4 — Implementation [IN PROGRESS]
- Completed: DB schema, API service, worker core, LangGraph checkpointer, co-located MCP server, graph executor, and console frontend (Tasks 1-7)
- Post-Task 7 additions: worker registry table (`0002_worker_registry.sql`), worker self-registration/heartbeat/deregistration, reaper stale-worker cleanup, `GET /v1/tasks` list endpoint, task list UI
- Remaining: AWS infrastructure and containerization (Task 8)
- Source of truth: `docs/implementation_plan/phase-1/progress.md`

### Stage 5 — Validation [NOT STARTED]
- End-to-end crash-recovery demo
- Performance testing against scaling numbers
- Record demo video

### Stage 6 — Launch / Publish [NOT STARTED]
- README with setup instructions
- Blog post about one hard problem solved
- Push to GitHub
