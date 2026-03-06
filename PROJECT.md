# Cloud-Native Persistent Agent Runtime

## 1. Overview

Most AI agent frameworks treat execution as ephemeral—running in-process with state living in memory. A crash means starting over. This works for demos but fails for production workloads where agents run for hours, coordinate across steps, and cost real money per LLM call.

This project delivers a **cloud-native, serverless durable execution runtime designed specifically for AI agents**. It separates agent identity (state) from execution (compute), enabling developers to submit long-running tasks without provisioning or managing the underlying worker infrastructure. It solves three critical problems:

1. **Non-deterministic execution:** LLM calls return different results each time, necessitating checkpoint-resume rather than deterministic replay.
2. **Unbounded memory bloat:** Agent memory grows with every interaction, requiring distilled long-term memory with compaction.
3. **Cost runaway:** Per-token pricing demands cost-aware scheduling and strict budget enforcement.

---

## 2. How This Differs From Existing Systems

| Feature | Temporal | LangGraph Platform | Restate | Azure Durable Functions | This Project |
|---------|----------|--------------------|---------|-------------------------|--------------|
| Execution model | Event-sourced deterministic replay | Graph execution with checkpointing | Journal-based replay | Checkpoint-resume with orchestrator constraints | **LangGraph graphs + durable lease-based execution (database-as-queue, distributed reaper, crash recovery)** |
| Memory model | Bounded workflow state | Conversation history | Key-value per virtual object | Orchestrator state (serializable) | **LangGraph state checkpoints (per-task) + append-only long-term memory with compaction (Phase 2)** |
| Cost awareness | None | None | None | Consumption-based billing (infra only) | **Per-node cost tracking with budget enforcement** |
| Infrastructure model | Self-hosted or Cloud | Managed platform (opinionated) | Self-hosted or Cloud | Azure-only managed | **Self-hosted, cloud-agnostic runtime you own — uses LangGraph for agent logic, owns the infra layer (queuing, leases, retries, dead letter, cost tracking)** |

**Why not just use Temporal?** 
Temporal requires deterministic orchestration logic. AI agents violate this because the LLM inherently decides the next step and its outputs vary. Temporal's workflow state is also bounded, while agent memory grows unboundedly. Finally, Temporal has no built-in cost model. At scale, where LLM calls can cost $0.10+ each, cost-aware scheduling is mandatory, not optional.

---

## 3. User Stories

### As an AI application developer:
- I want to submit a long-running multi-step task and have it execute reliably without babysitting.
- I want my agent's progress to survive worker crashes and seamlessly resume from the last checkpoint.
- I want full visibility into execution history—every step, input, output, latency, and cost.
- I want to set strict task budgets so a runaway agent doesn't drain LLM credits.

### As a platform operator (Agent-as-a-Service provider):
- I want to offer a serverless "Agent-as-a-Service" where customers submit tasks without managing underlying compute or worker infrastructure.
- I want to run multiple agents concurrently with fair resource sharing and horizontal scaling.
- I want dead-lettered tasks to be visible, investigateable, and re-drivable on behalf of customers.
- I want alerts for stuck tasks, excessive retries, or budget breaches to protect my margins.

---

## 4. Core Concepts

### Three-Component Model

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

- **Agent:** Identity and configuration stored in the database. An agent is data, not a process—it never "goes down." It defines persona, models, tools, memory, and budgets.
- **Task:** A unit of work belonging to one specific agent. The agent's config dictates how the task is executed.
- **Step:** A checkpoint within a task (e.g., `llm_call`, `tool_call`). Steps enable resume-from-checkpoint after crashes.
- **Worker:** A stateless process that claims tasks and executes steps. It loads the agent's config to "become" that agent. If a worker crashes, another loading the same config continues seamlessly.

In Phase 1, `agent_id` is a string field on Task with agent config stored inline. In Phase 2, Agent becomes a first-class entity in the database.

### Key Mechanisms

- **Lease-based ownership** — Workers hold time-bounded leases on tasks. Heartbeats extend leases. Expired leases are reclaimed by a reaper. Prevents both orphaned tasks and dual execution.
- **Checkpoint-resume** (not event-sourced replay) — On crash recovery, find the last completed step and continue from there. No determinism constraints. Chosen because LLM non-determinism makes Temporal-style replay unsuitable.
- **Idempotency via LangGraph checkpointing** — In Phase 1, LangGraph's `BaseCheckpointSaver` backed by PostgreSQL ensures each super-step is checkpointed before the next begins. On crash recovery, LangGraph resumes from the last checkpoint — no duplicate node execution. Transactional outbox is reserved for queue migration in Phase 2 (PostgreSQL -> SQS FIFO).
- **Database-as-queue** (Phase 1) — Tasks are stored and claimed from the same database atomically, eliminating the dual-write problem between a separate queue and state store.
- **Two-level memory** — LangGraph graph-state checkpoints in PostgreSQL natively include conversation history within a task. Long-term memory is distilled knowledge across tasks, stored as append-only entries in S3 with periodic compaction.
- **Error and retry model** — Steps that fail are retried with exponential backoff (1s, 2s, 4s). Non-retryable errors (4xx from LLM API, invalid tool definition) skip retries and fail immediately. Retry is per-task (default max 3 retries), resuming from the last completed step. Tasks exceeding max retries are moved to dead letter. Budget exceeded → task pauses (not fails), allowing manual intervention or budget increase.

---

## 5. Developer Experience & Integration

**The Integration Story:** This project leverages **LangGraph** as the underlying execution framework but replaces the infrastructure deployment burden. You don't write custom `while` loops or manual checkpointing logic; developers write standard LangGraph `StateGraph` definitions (nodes and edges). The Worker Service loads your graph and calls `graph.astream()`, while a custom `PostgresDurableCheckpointer` durably syncs the state to your database. The runtime owns the durable execution loop (queuing, leases, retries, budgeting) securely wrapping the LangGraph execution.

**Submitting a Task:**
```json
// POST /v1/tasks
{
  "agent_id": "support_agent_v1",
  "worker_pool_id": "cust_vpc_pool_99", // Routes to customer's private worker
  "input": "Refund user 123 for their last order",
  "budget": { 
    "max_usd": 0.50,
    "max_steps": 15
  }
}
```

---

## 6. Architectural Tradeoffs — Positions Taken

| Tradeoff | Decision | Rationale |
|----------|----------|-----------|
| Checkpoint-resume vs event-sourced replay | **Checkpoint-resume** | LLM calls are non-deterministic; replay would produce different results. |
| Strong vs eventual consistency | **Strong consistency on the execution path** | Prevents dual execution after worker crashes. Eventual consistency only for observability reads. |
| Database-as-queue vs separate queue | **Database-as-queue for Phase 1** | Eliminates dual-write problem. PostgreSQL handles 5K-10K claims/sec. |
| Standalone runtime vs Temporal application | **Standalone runtime** | AI-specific problems need control over the execution loop. Better portfolio signal. |
| Tool side-effect containment vs flexibility | **Step-level idempotency + downstream idempotency keys** | Full sandboxing too restrictive for MVP. Idempotent tools can retry safely; non-idempotent recovery routes to dead letter for manual review. |

---

## 7. Technology Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Language | **Java (Core) & Python (Workers)** | Java for the high-concurrency central API and state management. Python for the workers to integrate easily with the AI ecosystem (LangChain, SDKs, MCP). |
| State store (Phase 1) | **PostgreSQL (Aurora Serverless v2)** | `FOR UPDATE SKIP LOCKED` eliminates need for separate queue. |
| Queue (Phase 2) | **SQS FIFO** | Per-agent ordering via message group ID. Transactional outbox from PostgreSQL. |
| Compute | **ECS Fargate** | Horizontally scalable, no cluster management. |
| LLM integration | **Bedrock + OpenAI/Anthropic APIs** | Bedrock for AWS-native signal; direct APIs for practical coverage. |
| Observability | **OpenTelemetry → CloudWatch** | Vendor-neutral instrumentation, low-ops backend. |
| IaC | **CDK (TypeScript)** | Entire stack defined in one repo. |

---

## 8. Phases

### Phase 1 — Durable Execution MVP (4-6 weeks)

**Goal:** Prove that tasks survive worker crashes and resume correctly from the last checkpoint.

**Scope:**
- Task submission API (REST)
- LangGraph-based execution engine with lease-based ownership and heartbeats
- Custom `PostgresDurableCheckpointer` for durable graph state
- Reaper for expired leases and dead letter handling
- Per-node cost tracking via LangGraph event streaming
- OpenTelemetry traces and key metrics

**Demo scenario:**
1. Submit a multi-step research task
2. Worker executes several LangGraph super-steps (agent → tools → agent → ...)
3. Kill the worker mid-execution
4. Lease expires, reaper reclaims the task
5. New worker picks up the task; LangGraph resumes from last checkpoint
6. Task completes successfully — previously checkpointed nodes are not re-executed
7. Query full checkpoint history with timing and cost breakdown

**Out of scope:** Multi-agent scheduling, memory compaction, approval workflows, UI, multi-tenancy.

### Phase 2 — Multi-Agent & Cost-Aware Scheduling (4-6 weeks)

**Goal:** Support multiple agents with fair scheduling and budget enforcement.

**Scope:**
- Agent entity with configuration, concurrency limits, and memory reference
- Cost-aware scheduler: per-agent budgets, tasks paused (not failed) when budget exceeded
- Fair scheduling: weighted fair queuing to prevent agent monopolization
- Worker backpressure: pull-based concurrency semaphore
- Memory compaction: LLM-based summarization of long-term agent memory
- Private Workers (BYOW - Bring Your Own Worker): Bridging the gap between a fully hosted SaaS and enterprise security. Tasks route to customer-deployed workers in their VPC for secure access to internal APIs and MCP servers, while the runtime still manages orchestration and billing.
- SQS FIFO migration via transactional outbox (if needed)

### Future Directions (Post Phase 2)

- Cross-agent coordination (request-response between agents, deadlock detection)
- Agent versioning and rolling updates
- Human-in-the-loop approval workflows
- Execution history replay debugger

---

## 9. Scaling Outlook

**Assumptions:** Each agent has 1 active task, 20% of agents executing at any moment, ~10 steps/task, ~5s per step (LLM latency dominates).

| Scale | Steps/sec | DB ops/sec | First Bottleneck |
|-------|-----------|------------|------------------|
| 1K agents | 40 | 160 | Nothing — system is idle |
| 10K agents | 400 | 1,600 | LLM API rate limits |
| 50K agents | 2,000 | 8,000 | LLM API cost ($72K/hour) |
| 100K agents | 4,000 | 16,000 | Step history storage (500GB/day) |

**Key insight:** The runtime is not the scaling bottleneck — the LLM API (rate limits and cost) is. This validates investing in cost-aware scheduling.

---

## 10. What This Project Is Not

- Not a chatbot framework — no conversation UI, no streaming responses
- Not a reimplementation of Temporal — it solves AI-agent-specific problems (see Section 2)
- Not a prompt engineering playground — the focus is execution infrastructure
- Not a replacement for LangGraph — this project *uses* LangGraph under the hood to define the agent's logic, acting as the durable execution layer beneath it.
- Not an infrastructure management burden — developers define the agent and submit tasks; the runtime handles the compute, scaling, and crash recovery.

---

## 11. Related Documents

- [design/PHASE1_DURABLE_EXECUTION.md](./design/PHASE1_DURABLE_EXECUTION.md) — Phase 1 design: architectural context, entity model, API contract, DB schema, sequence diagrams, lease protocol, idempotency
- [design/PHASE2_MULTI_AGENT.md](./design/PHASE2_MULTI_AGENT.md) — Phase 2 design: Agent entity, cost-aware scheduling, memory compaction, private workers
- [design/DESIGN_NOTES_PHASE2.md](./design/DESIGN_NOTES_PHASE2.md) — Phase 2+ reference material: full Agent entity, long-term memory model, scaling analysis, DynamoDB design

---
