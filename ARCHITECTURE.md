# Architecture

Cloud-Native Persistent Agent Runtime — durable execution infrastructure for AI agents.

## Key Architecture Decisions

- **Agent = data, not a process.** Agent config (persona, model, tools, memory, budget) lives in DB. Workers load agent config to "become" that agent. Agents never "go down."
- **Task belongs to one agent** (immutable). Worker is the stateless process that executes tasks.
- **Checkpoint-resume, not event-sourced replay.** LLM calls are non-deterministic — Temporal-style replay doesn't work.
- **Database-as-queue (Phase 1).** PostgreSQL `FOR UPDATE SKIP LOCKED`. Eliminates dual-write problem.
- **Strong consistency on the execution path.** Lease ownership plus database locks protect execution; the task `version` field is for lifecycle transitions and auditing, not per-checkpoint optimistic concurrency.
- **Phase 1 recovery model is conservative.** Previously checkpointed nodes are not re-executed, but an interrupted in-flight node may be re-executed in full after crash recovery. Phase 1 enforces idempotent-only tools at submission time; non-idempotent tool guards are deferred to Phase 2.
- **Tools via MCP protocol.** Phase 1 uses a co-located MCP server for built-in tools. Phase 2 introduces the Custom Tool Runtime (BYOT): customers upload MCP server containers, the platform runs them in isolated compute within the same VPC.
- **Phase 1 scope excludes subgraphs and budget enforcement.** Phase 1 uses a single top-level LangGraph only; budget enforcement is deferred to Phase 2.
- **LLMs are stateless.** Memory is simulated by assembling prompts from stored data (agent config + long-term memory from S3 + step history from PostgreSQL).
- **Two-level memory:** step checkpoints in PostgreSQL double as conversation history within a task. Long-term memory is distilled knowledge across tasks, stored as append-only entries in S3 with compaction.

## Entity Model

```
┌─────────────┐       ┌─────────────┐       ┌─────────────┐
│   Agent      │       │    Task      │       │   Worker     │
│  (identity)  │       │   (work)     │       │  (process)   │
│              │       │              │       │              │
│ Lives in DB  │◄──────│ belongs to   │       │ Claims task  │
│ Never dies   │  1:N  │ one agent    │◄──────│ Loads agent  │
│ Has persona  │       │ Has steps    │       │ Executes     │
│ Has memory   │       │ Has state    │       │ Can crash    │
│ Has config   │       │              │       │ Replaceable  │
└─────────────┘       └─────────────┘       └─────────────┘
```

- **Agent:** Identity and configuration stored in the database. Defines persona, models, tools, memory, and budgets.
- **Task:** A unit of work belonging to one agent. Contains input, status, checkpoint history, and cost tracking.
- **Worker:** A stateless process that claims tasks via lease and executes them. If it crashes, another worker continues from the last checkpoint.

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Core runtime | **Java (Spring Boot)** | High-concurrency API and state management |
| Workers | **Python** | AI ecosystem integration (LangChain, SDKs, MCP) |
| Console & IaC | **TypeScript (React 19 + Vite + Tailwind/shadcn/ui, CDK)** | Frontend SPA and infrastructure-as-code |
| State store (Phase 1) | **PostgreSQL (Aurora Serverless v2)** | `FOR UPDATE SKIP LOCKED` eliminates need for separate queue |
| Queue (Phase 2) | **SQS FIFO** | Per-agent ordering via message group ID |
| Compute | **ECS Fargate** | Horizontally scalable, no cluster management |
| Agent execution | **LangGraph** | Orchestration primitives with custom durable checkpointer |
| LLM integration | **LangChain `init_chat_model`** | Anthropic, OpenAI, Google, Bedrock; auto-discovered from API keys |
| Observability (LLM) | **Langfuse (customer-owned)** | Per-task execution traces, cost/token tracking |
| Observability (platform) | **CloudWatch** | Structured logs, platform metrics, alerts |

## Service Topology

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│   Console (SPA)  │────▶│  API Service      │◄────│  Worker Service  │
│   React 19       │     │  Spring Boot      │     │  Python          │
│   Port 5173      │     │  Port 8080        │     │  Polls for tasks │
└──────────────────┘     └────────┬─────────┘     └────────┬─────────┘
                                  │                         │
                                  ▼                         ▼
                         ┌──────────────────┐     ┌──────────────────┐
                         │  PostgreSQL       │     │  MCP Server      │
                         │  State + Queue    │     │  (co-located)    │
                         │  Checkpoints      │     │  Built-in tools  │
                         └──────────────────┘     └──────────────────┘
```

- **Console** → React SPA for monitoring, task dispatch, and Langfuse endpoint management
- **API Service** → REST API for task submission, status, checkpoints, cancellation, dead-letter, redrive, and model/provider management
- **Worker Service** → Claims tasks from PostgreSQL, executes LangGraph workflows, writes checkpoints, heartbeats leases
- **MCP Server** → Co-located with worker, serves built-in idempotent tools (`web_search`, `read_url`, `calculator`)
- **PostgreSQL** → Single source of truth: task state, queue, checkpoints, worker registry, Langfuse endpoints, model/provider registry

## Design Documents

- [Phase 1 Design](docs/design-docs/phase-1/design.md) — Entity model, API contract, DB schema, lease protocol, idempotency
- [Phase 2 Design](docs/design-docs/phase-2/design.md) — Agent entity, cost-aware scheduling, long-term memory, Custom Tool Runtime
- [Phase 2 Track 1](docs/design-docs/phase-2/track-1-agent-control-plane.md) — Agent control plane detail
- [Phase 3+ Notes](docs/design-docs/phase-3-plus/design-notes.md) — Scaling analysis, queue/storage evolution, DynamoDB design
- [Langfuse Integration](docs/design-docs/langfuse/design.md) — Customer-owned Langfuse integration
