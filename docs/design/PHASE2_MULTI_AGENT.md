# Phase 2 Design — Multi-Agent, Memory, and Cost-Aware Scheduling

**Status:** Not started.

**Goal:** Promote Agent to a first-class entity and extend the Phase 1 durable execution runtime with multi-agent scheduling, long-term memory, richer operational history, and customer-provided tools.

---

## Scope

- Agent as first-class entity (replaces inline `agent_config_snapshot` on new tasks)
- Agent CRUD and configuration management
- Per-agent concurrency limits and fair scheduling
- Cost-aware scheduling: per-agent budgets, tasks paused (not failed) when budget is exceeded
- Long-term memory: append-only S3 entries with LLM-based compaction
- Custom Tool Runtime (BYOT): customer-provided MCP servers running in platform-managed isolated containers
- `waiting_for_approval` task status for human-in-the-loop workflows
- Non-idempotent tool guards: `idempotent: true|false` annotation on MCP tool schema, checkpoint-before-call for mutable tools, dead-letter on re-execution after crash
- Redrive checkpoint rollback: `rollback_last_checkpoint` option on `POST /redrive`
- Mid-node task cancellation during in-flight LLM/tool calls
- Append-only task retry/error event history (`task_events`)
- Runtime secret-management hardening: move from Phase 1 env vars to AWS Secrets Manager backed retrieval and rotation

**Still out of scope for Phase 2:** queue migration beyond PostgreSQL, DynamoDB redesign, and other scale-driven architectural changes. Those remain in Phase 3+ notes.

---

## 1. Agent Entity

In Phase 1, agent config is snapshotted inline on the Task record. In Phase 2, Agent becomes a first-class entity with its own table, enabling per-agent concurrency limits, fair scheduling, and budget enforcement.

```
agent_id:             string
config:               JSON
  ├── system_prompt:  string ("you are a research assistant that...")
  ├── model:          string ("claude-sonnet-4-6")
  ├── temperature:    float (0.7)
  └── allowed_tools:  []string (["web_search", "read_url", "calculator"])
memory_ref:           string (S3 prefix for long-term memory, e.g., s3://bucket/memory/agent123/)
max_concurrent_tasks: int (default 5)
task_ttl_seconds:     int (default 86400)
budget_max_per_task:  int (microdollars, default 500000 = $0.50)
budget_max_per_hour:  int (microdollars, default 5000000 = $5.00)
status:               enum (active | paused | disabled)
```

An agent is a database record that defines identity. It stores what the agent is (persona, model, tools, budget, memory pointers) but not where it runs. Any worker can act as any agent by loading this config.

### Phase 1 to Phase 2 transition

- New task submissions reference `agent_id` and read config from the Agent table
- The task still snapshots the resolved agent config at creation time for execution stability and auditability
- Existing Phase 1 semantics remain: a Task belongs to exactly one Agent, fixed at creation time

---

## 2. Cost-Aware Scheduling

Phase 2 adds scheduler behavior that reasons about agent fairness and budget, not just queue order.

### Scheduling goals

- Prevent a single hot agent from monopolizing worker capacity
- Enforce `max_concurrent_tasks` per agent
- Pause tasks when budgets are exhausted instead of failing them
- Preserve the Phase 1 durable execution guarantees during pause/resume transitions

### Budget model

- `budget_max_per_task`: upper bound for a single task
- `budget_max_per_hour`: rolling hourly budget per agent
- Budget exhaustion pauses tasks rather than dead-lettering them
- A separate API increases budget or resumes paused tasks

### Required scheduler behavior

- Track cumulative task cost across checkpoints and completed tasks
- Prefer claimable tasks from agents that are under concurrency and budget limits
- Surface paused state clearly in status APIs and dashboards

---

## 3. Agent Memory Model

### Why memory is an infrastructure problem

LLMs are stateless. Every LLM call is assembled from stored data:

```
┌─────────────────────────────────────────────┐
│ LLM API call = assembled prompt             │
│                                             │
│  1. System prompt (agent config)            │
│  2. Long-term memory (loaded from S3)       │
│  3. Conversation history (prior steps)      │
│  4. Current step input                      │
└─────────────────────────────────────────────┘
```

This means memory is directly a cost and latency problem. Every byte of memory stored is eventually prompt context, which means more tokens billed and slower responses. At the context-window limit, memory must be truncated or summarized.

### Two storage levels

**Step checkpoints (per-task)**

Phase 1 checkpoints already provide the conversation history for the current task. They remain the short-lived, task-local memory used to resume execution and reconstruct recent context.

**Long-term memory (per-agent, across tasks)**

Distilled knowledge that persists between tasks: facts learned, user preferences, domain knowledge. This is not raw conversation history. It is structured knowledge extracted from completed tasks.

Long-term memory uses an append-only model in S3 so concurrent tasks do not conflict:

```
s3://bucket/memory/agent123/
  ├── compacted-002.json
  ├── entry-015.json
  ├── entry-016.json
  └── entry-017.json
```

### Read flow

1. Load the latest compacted snapshot
2. Load entries created after that snapshot
3. Concatenate compacted snapshot plus recent entries into the memory context used by the prompt

### Write flow

1. Task runs using Phase 1 checkpoints as short-term memory
2. Task completes
3. A post-task extraction step distills durable learnings
4. The distilled result is written as a new append-only S3 object

### Compaction strategy

- User-initiated compaction: `POST /agents/{agent_id}/compact`
- Periodic compaction when the agent has no active tasks and uncompacted memory exceeds a threshold
- Compaction itself runs as a durable task through the runtime

### Why not persist raw prompt history forever

Raw conversation history grows too quickly in cost, latency, and token footprint. Long-term memory must be distilled, not copied forward verbatim.

---

## 4. Custom Tool Runtime (BYOT — Bring Your Own Tools)

Phase 2 replaces the former "Private Workers (BYOW)" idea with customer-provided MCP tool runtimes. Customers provide tools, not worker processes.

Tasks specify a `worker_pool_id` which doubles as a tool runtime routing key. By default, tasks use the `"shared"` pool with built-in tools served by the co-located MCP server. Customers can register custom MCP server containers via a tool registration API, and the platform runs them in isolated ECS tasks within the platform's VPC.

### Architecture

- Customer uploads an MCP server container image or code bundle
- The platform deploys it as an isolated ECS task within the platform's VPC
- The Worker Service calls it over private networking
- The control plane keeps durable execution, checkpointing, leases, retries, and redrive semantics

### Security model

- Customer MCP servers are never exposed to the public internet
- VPC security groups restrict traffic to Worker Service -> MCP server only
- Customer MCP servers have no database access and no access to other customers' containers
- The MCP server is a pure tool executor; all workflow durability remains in the control plane

### Non-idempotent tool safety

Phase 1 avoids mutable tools entirely. Phase 2 adds explicit idempotency metadata and control-plane safeguards:

- `idempotent: true|false` on tool schema
- checkpoint-before-call for mutable tools
- dead-letter rather than blind re-execution if a crash occurs after an unsafe side effect

### Customer simplicity

Customers only implement MCP tool handlers. They do not need to understand durable execution, leases, reapers, or the internal DB schema.

---

## 5. Execution Audit History

Phase 1 keeps mutable summary fields on `tasks` such as `status`, `retry_count`, `last_error_code`, and `last_error_message`. This is sufficient for recovery and basic status reporting, but not as a full audit trail.

Phase 2 should add a separate append-only task event history so the system can expose both:

- current state: the latest truth on the `tasks` row
- historical timeline: retries, lease-expiry recoveries, dead-letter transitions, redrives, and recoveries

### Proposed entity

```
event_id:             UUID
tenant_id:            string
task_id:              UUID
agent_id:             string
event_type:           enum (
                         task_submitted |
                         task_claimed |
                         task_retry_scheduled |
                         task_reclaimed_after_lease_expiry |
                         task_dead_lettered |
                         task_redriven |
                         task_completed
                       )
status_before:        enum (nullable)
status_after:         enum (nullable)
worker_id:            string (nullable)
error_code:           string (nullable)
error_message:        string (nullable)
details:              JSON
created_at:           timestamp
```

### Semantics

- `tasks.last_error_code` and `tasks.last_error_message` remain summary fields only
- they are cleared on successful completion or redrive
- `task_events` is the durable audit timeline for customers and operators
- status/dead-letter list APIs can stay fast by reading `tasks`
- detailed task history comes from `task_events`

---

## 6. LLM Credential Model and Secret Management

### Platform-owns-keys (decided)

The platform holds all LLM provider API keys centrally. Users never provide their own provider credentials. The platform bills users based on per-checkpoint cost data tracked in the `models` database table, enforced through the budget model in Section 2.

This decision aligns with the cost-aware scheduling design: the platform must control LLM spending to enforce `budget_max_per_task` and `budget_max_per_hour`. Platform-owned keys also enable centralized rate-limit management and negotiated enterprise pricing with providers.

**BYOK (Bring Your Own Key) is explicitly deferred to Phase 3+.** See [DESIGN_NOTES_PHASE3_PLUS.md](./DESIGN_NOTES_PHASE3_PLUS.md).

### Centralized model and key registry

A **Python discovery script** runs at system startup. It reads the platform's API keys from its environment (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.), then queries each provider's model listing API (e.g., Anthropic and OpenAI both expose `GET /v1/models`) to discover available chat models. It upserts the keys and discovered models into the shared PostgreSQL database (`provider_keys` and `models` tables). Models are marked active only if their provider's API key is present and the provider confirms the model's availability.

The **API Service** reads active models from the database. It validates task submissions and serves `GET /v1/models` to the Console.

The **Worker Service** is a stateless executor. When it claims a task, it reads the required API key from the `provider_keys` table and passes it to LangChain's `init_chat_model`. Workers do not need LLM API keys in their own environment.

**Multi-instance safety:** The discovery script acquires a PostgreSQL advisory lock before syncing, ensuring only one instance writes at a time.

### Discovery service evolution

Phase 1 runs discovery as a startup script (like DB migrations). The database schema (`provider_keys` and `models` tables) is the stable contract between the writer (discovery component) and readers (API Service, Workers, Console). This contract enables a clean evolution path:

- **Phase 1:** Startup script — runs once at deploy, reads env vars, queries providers, writes to DB, exits.
- **Phase 2:** Long-running service — runs continuously, periodically re-syncs models, picks up key rotations from Secrets Manager without restarting any other service. Enables zero-downtime key rotation.
- **Phase 3+:** Same service extended with per-tenant credential vaults for BYOK support.

The readers never change — only the writer evolves.

### Secret management hardening (Phase 2)

Phase 1 stores API keys in the `provider_keys` database table (plaintext, acceptable for local development and early production).

Phase 2 hardens this by migrating to AWS Secrets Manager:

- API Service reads keys from Secrets Manager instead of environment variables
- Workers read keys from Secrets Manager instead of the `provider_keys` table
- `provider_keys` table is dropped; `models` table remains for pricing and availability
- rotation without restarting any services
- least-privilege IAM scoping for API Service, Worker Service, and customer tool runtimes

Even after migration to Secrets Manager, secrets remain operational configuration, not business data. They must never be stored in `tasks`, `checkpoints`, or `task_events`.

---

## 7. Reliability Additions

Phase 2 extends the Phase 1 recovery model with:

- cost runaway prevention through budget enforcement
- step-history retention and archival policy
- explicit pause/resume behavior
- safer handling for mutable tools and mid-node cancellation

---

## References

- [docs/PROJECT.md](../PROJECT.md) — phase definitions and scope
- [PHASE1_DURABLE_EXECUTION.md](./PHASE1_DURABLE_EXECUTION.md) — foundation this phase builds on
- [DESIGN_NOTES_PHASE3_PLUS.md](./DESIGN_NOTES_PHASE3_PLUS.md) — later-phase reference material
