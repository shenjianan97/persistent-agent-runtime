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
- Langfuse (customer-owned) — per-task LLM execution tracing, cost/token tracking, customer-facing observability; customers configure their own Langfuse endpoints via Settings page
- CloudWatch — platform health metrics, structured logs, alerts (operator-facing)
- LangChain `init_chat_model` — LLM integration (Anthropic, OpenAI, Google, Bedrock; providers auto-discovered from configured API keys)

## Documents

| File | Purpose |
|------|---------|
| docs/PROJECT.md | High-level project overview: vision, user stories, phases, tradeoffs, tech stack |
| docs/design/PHASE1_DURABLE_EXECUTION.md | Phase 1 design: architectural context, entity model, API contract, DB schema, sequence diagrams, lease protocol, idempotency, observability |
| docs/design/PHASE2_MULTI_AGENT.md | Phase 2 design: Agent entity, cost-aware scheduling, long-term memory, task event history, secret management hardening, Custom Tool Runtime / BYOT |
| docs/design/DESIGN_NOTES_PHASE3_PLUS.md | Phase 3+ reference material: scaling analysis, queue/storage evolution options, DynamoDB design, future tool integration |
| docs/implementation_plan/phase-1/plan.md | Phase 1 Orchestrator Plan detailing dependencies, AWS integration, and execution breakdown |
| docs/implementation_plan/phase-1/progress.md | Live tracking board for agent execution of Phase 1 |
| docs/implementation_plan/phase-1/agent_tasks/*.md | 9 single-responsibility execution templates for agents (Tasks 1-8 implementation, Task 9 observability follow-up) |
| docs/design/langfuse-customer-integration/ | Langfuse customer-owned integration design doc |
| docs/implementation_plan/langfuse-customer-integration/ | Langfuse customer integration orchestrator plan and 5 task specs |
| experiments/langgraph/plan.md | Proof of concept strategy to validate LangGraph checkpointer exceptions |

## Local Validation Notes

- For local testing, follow the repo's documented local development and validation workflow in `README.md` unless it is not feasible in the current environment.
- When validating background `Makefile` targets such as `make start`, `make status`, and `make stop`, prefer an interactive shell / PTY. Some non-interactive command runners reap or detach child processes when the parent command exits, which can make background-service checks look broken even when the `Makefile` logic is correct.

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
- Splitting Prompts: Generated 8 initial implementation task spec files (`task-1` through `task-8`) plus the follow-up Task 9 observability refinement spec
- Tracking: Created `docs/implementation_plan/phase-1/progress.md` for orchestrator execution tracking
- POC logic: Created LangGraph POC validation tasks in `experiments/langgraph/` to test assumptions

### Stage 4 — Implementation [DONE]
- Completed: DB schema, API service, worker core, LangGraph checkpointer, co-located MCP server, graph executor, console frontend, and AWS infrastructure/containerization (Tasks 1-8)
- Post-Task 7 additions: worker registry table (`0002_worker_registry.sql`), worker self-registration/heartbeat/deregistration, reaper stale-worker cleanup, `GET /v1/tasks` list endpoint, task list UI, multi-worker scaling (`make start-worker N=`, `make scale-worker N=`)
- Task 8 additions: AWS CDK app (`infrastructure/cdk/`) with Network/Data/Compute stacks, schema bootstrap custom resource, internal ALB + SSM access host, ECS services, scheduled-and-initial model discovery, service-owned Dockerfiles, and GitHub Actions coverage for CDK build/tests
- Follow-up fixes landed: canonical migration bundling from `infrastructure/database/migrations/`, initial model-discovery redeploy triggering, failure surfacing for the bootstrap invoke, access-host AMI architecture matching, Docker `platform: LINUX_AMD64` for ARM Mac cross-compilation, and CORS config disabled by default for same-origin ALB access
- Task 9 (Langfuse observability): Refactored from platform-hosted to customer-owned integration — `langfuse_endpoints` table, per-task endpoint CRUD API, worker resolves credentials per-task, checkpoint-based cost/token aggregation, Console Settings page for endpoint management, E2E test suite
- Console refresh: dashboard UX overhaul with action-oriented layout, scrollable panels, consistent dark-mode styling
- Source of truth: `docs/implementation_plan/phase-1/progress.md`

### Stage 5 — Validation [IN PROGRESS]
- Completed: local CDK build/test verification, console production build verification, worker/model-discovery entrypoint verification
- Completed: AWS account deployment validation — full stack deployed and end-to-end task execution verified (task submission via Console, worker pickup, tool call, completion with cost tracking)
- Deploy-time fixes applied: Docker `platform: LINUX_AMD64` for ARM-to-Fargate cross-compilation, CORS config skipped when no origins configured (same-origin ALB needs no CORS)
- Remaining: end-to-end crash-recovery demo, performance testing against scaling numbers, demo video

### Stage 6 — Launch / Publish [NOT STARTED]
- README with setup instructions
- Blog post about one hard problem solved
- Push to GitHub
