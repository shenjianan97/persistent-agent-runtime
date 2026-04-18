# Phase 2 Design — Multi-Agent, Memory, and Cost-Aware Scheduling

**Status:** In progress (Tracks 1, 2, 3, 4 complete; Tracks 5, 6, 7 upcoming).

**Goal:** Promote Agent to a first-class entity and extend the Phase 1 durable execution runtime with multi-agent scheduling, long-term memory, richer operational history, and customer-provided tools.

---

## Scope

- Agent as first-class entity (replaces inline `agent_config_snapshot` on new tasks)
- Agent CRUD and configuration management
- Per-agent concurrency limits and fair scheduling
- Cost-aware scheduling: per-agent budgets, tasks paused (not failed) when budget is exceeded
- Long-term memory: append-only S3 entries with LLM-based compaction
- Custom Tool Runtime (BYOT): customer-provided MCP servers running in platform-managed isolated containers
- `waiting_for_approval` and `waiting_for_input` task statuses for human-in-the-loop workflows
- GitHub integration for code agents: GitHub App for repo access, clone/push/PR workflow
- Context window management for long-running tasks: tiered in-task compaction so tool-call bloat does not push tasks into context-limit or cost-limit failure
- ~~Non-idempotent tool guards~~ (deferred to Phase 3+)
- Redrive checkpoint rollback: `rollback_last_checkpoint` option on `POST /redrive`
- Mid-node task cancellation during in-flight LLM/tool calls
- Append-only task retry/error event history (`task_events`)
- Runtime secret-management hardening: move from Phase 1 env vars to AWS Secrets Manager backed retrieval and rotation

**Still out of scope for Phase 2:** queue migration beyond PostgreSQL, DynamoDB redesign, and other scale-driven architectural changes. Those remain in Phase 3+ notes.

---

## Planning Tracks

Phase 2 spans several loosely coupled subsystems. To keep implementation planning manageable, treat the work as seven planning tracks with clear dependencies.

### Track 1 — Agent Control Plane

Establish Agent as a first-class entity and make it the source of truth for runtime configuration.

- Agent storage model and lifecycle
- Agent CRUD and configuration management
- Task submission refactor: submit by `agent_id`, resolve from the Agent table, then snapshot resolved config onto the task for execution stability and auditability
- Console/API flows for selecting and managing agents

Primary design coverage:
- [Section 1. Agent Entity](#1-agent-entity)

### Track 2 — Runtime State Model

Extend the task lifecycle beyond Phase 1's queued/running/completed/dead-letter flow so Phase 2 features share one coherent state machine.

- New durable pause states such as `waiting_for_approval` and `waiting_for_input`
- Pause/resume/redrive/cancel semantics
- Append-only task event history (`task_events`)
- Status API and Console updates to expose richer runtime state

Primary design coverage:
- [Section 5. Execution Audit History](#5-execution-audit-history)
- [Section 7. Human-in-the-Loop Input](#7-human-in-the-loop-input)
- [Section 8. Reliability Additions](#8-reliability-additions)

### Track 3 — Scheduler and Budgets

Replace simple FIFO claiming with scheduling that accounts for agent-level concurrency and spend.

- Per-agent concurrency limits
- Fair scheduling across agents
- Budget accounting and budget-based pause behavior
- Console/API surfacing for paused and budget-constrained tasks

Primary design coverage:
- [Section 2. Cost-Aware Scheduling](#2-cost-aware-scheduling)

### Track 4 — Custom Tool Runtime (BYOT)

Enable customer-provided MCP tool servers so agents can use tools beyond the built-in set.

- Tool server registration and management (by HTTP URL)
- Worker MCP client integration for tool discovery and invocation at task execution time
- Agent config extension for referencing custom tool servers
- Bearer token authentication for MCP servers that require it

**Note:** Credential hardening (Secrets Manager migration, unified secret resolver) was originally part of this track but has been deliberately deferred to Phase 3+. See [design-notes.md, Section 8](../phase-3-plus/design-notes.md).

Primary design coverage:
- [Section 4. Custom Tool Runtime (BYOT — Bring Your Own Tools)](#4-custom-tool-runtime-byot--bring-your-own-tools)

### Track 5 — Memory

Add per-agent cross-task memory so completed tasks produce distilled, searchable entries that can be attached to future tasks on demand.

- Opt-in per-agent memory store (`agent_memory_entries`) in Postgres with hybrid BM25 + vector search via `pgvector`
- Final LangGraph node distills each completed task into one memory entry
- Agent `memory_note`, `memory_search`, and `task_history_get` tools
- Customer attach-at-submission flow + Console browse and delete

**Design scope change:** The original design (Section 3 below) described auto-loaded long-term memory with S3 append-only storage and periodic compaction — a personal-assistant-shaped model that does not fit the managed-runtime use case. The track-level design rescopes memory to a single Postgres-backed store with explicit-only retrieval, no auto-injection, and no promotion/compaction. In-task context compaction (the other reason memory mattered in the old sketch) has been split out as Track 7. See [track-5-memory.md](./track-5-memory.md) for the current design.

**Note:** Human approval and freeform input workflows were originally scoped here but were delivered as part of Track 2 (Runtime State Model) alongside the `waiting_for_approval` / `waiting_for_input` pause states. Non-idempotent tool safeguards were deferred to Phase 3+.

Primary design coverage:
- [track-5-memory.md](./track-5-memory.md) — current design
- [Section 3. Agent Memory Model](#3-agent-memory-model) — original sketch, retained for historical context

### Track 6 — GitHub Integration

Enable code agents to access customer repositories and deliver results as pull requests, following the industry-standard pattern used by all major cloud coding agents (Devin, Codex, Jules, Copilot, etc.).

- GitHub App installation for org/repo access
- Short-lived installation tokens (no long-lived secrets)
- Code agent workflow: clone repo into sandbox → work → push branch → open PR
- Future: GitLab / Bitbucket support

**Depends on:** The cross-cutting [agent-capabilities](../agent-capabilities/design.md) work (E2B sandbox, artifact storage) shipping first — sandbox provides the execution environment where git operations happen.

Primary design coverage:
- Detailed design TBD (will be developed as a dedicated design doc when this track is planned)

### Track 7 — Context Window Management

Keep long-running tasks viable by bounding the in-task message-history growth that otherwise pushes tasks into context-limit or cost-limit failure.

- Tiered in-task compaction (tool-result clearing, tool-call arg truncation, and retrospective LLM summarization)
- Pre-LLM-call transforms inside the LangGraph executor loop — no new service, no new schema
- Platform-level and per-agent thresholds; agent-opt-out path for workloads that need raw history

**Why this is a separate track, not part of Track 5:** Memory (Track 5) is a cross-task store. Context management is a within-task transform. They share zero schema, zero API, zero UI. Bundling them would bloat both and couple independent rollout schedules. This track is specifically the work described in [GitHub issue #50](https://github.com/shenjianan97/persistent-agent-runtime/issues/50).

**Status:** Proposed, design TBD — see [track-7-context-window-management.md](./track-7-context-window-management.md) for the stub and the brainstorm gate.

Primary design coverage:
- Detailed design pending its own brainstorming and design pass

### Recommended Planning Order

For implementation planning, the safest order is:

1. Track 1 — Agent Control Plane ✅
2. Track 2 — Runtime State Model ✅
3. Track 3 — Scheduler and Budgets ✅
4. Track 4 — Custom Tool Runtime (BYOT) ✅
5. **Agent Capabilities (cross-cutting)** — see [agent-capabilities/design.md](../agent-capabilities/design.md):
   - AC Track 1 — Output Artifact Storage ✅
   - AC Track 2 — E2B Sandbox & File Input ✅
   - **AC Track 3 — Coding-Agent Primitives (proposed; must land before Phase 3)**
6. Track 7 — Context Window Management (recommended ahead of Track 5: long tasks must be able to finish before cross-task memory is useful)
7. Track 5 — Memory
8. Track 6 — GitHub Integration

Phase 2 Tracks 1–4 and Agent Capabilities Tracks 1 & 2 are complete — the platform can now run coding and document-processing workloads end-to-end with sandbox execution and artifact storage.

**AC Track 3 is gating for Phase 3.** The Track 2 sandbox tool surface is sufficient to run a script but not to iterate on a codebase — every edit re-sends the full file, every search goes through `sandbox_exec` with no output cap, long-running processes block the tool slot. Phase 3 work (batch APIs, webhooks, structured output, scaling) should be built on top of a mature coding-agent tool surface rather than ship on top of token-burning primitives that would then need to be rolled back later. See [agent-capabilities/design.md#track-3-coding-agent-primitives-proposed](../agent-capabilities/design.md) for the detailed proposal.

Tracks 5 (Memory), 6 (GitHub Integration), and 7 (Context Window Management) can be sequenced alongside AC Track 3 as independent initiatives — they have no blocking dependency in either direction. Track 7 is recommended ahead of Track 5 on the grounds that cross-task memory is less useful if long-running tasks cannot complete.

---

## 1. Agent Entity

In Phase 1, agent config is snapshotted inline on the Task record. In Phase 2, Agent becomes a first-class entity with its own table, enabling per-agent concurrency limits, fair scheduling, and budget enforcement.

```
tenant_id:            string (logical isolation key, used with agent_id as composite PK)
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

An agent is a database record that defines identity. The true unique identity is `(tenant_id, agent_id)`. It stores what the agent is (persona, model, tools, budget, memory pointers) but not where it runs. Any worker can act as any agent by loading this config.

### Phase 1 to Phase 2 transition

- **Task Status ENUM Expansion:** The Phase 1 `tasks.status` ENUM (`queued, running, completed, dead_letter`) must be altered or constraint-updated to include the new pause states (`waiting_for_approval`, `waiting_for_input`, and `paused`).
- **Foreign Keys:** `tasks` and `task_events` should likely gain a composite foreign key referencing `agents(tenant_id, agent_id)`. To preserve task history when an agent is removed, soft deletes (`status = 'disabled'`) on the `agents` table should be used rather than destructive `DELETE`s to avoid cascaded execution history loss.
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
- Prefer claimable tasks from agents that are under concurrency and budget limits. **Note:** Enforcing `max_concurrent_tasks` dynamically within the worker's `FOR UPDATE SKIP LOCKED` claim query can create a severe database bottleneck under load. The design should evaluate maintaining a fast-path materialized counter of running tasks per agent, or enforcing this primarily at submission/requeue time.
- Surface paused state clearly in status APIs and the customer-facing Console

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

Deferred to Phase 3+. See [design-notes.md, Section 9](../phase-3-plus/design-notes.md).

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
                         task_completed |
                         task_paused |
                         task_resumed |
                         task_approval_requested |
                         task_approved |
                         task_rejected |
                         task_input_requested |
                         task_input_received |
                         task_cancelled
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
- `task_cancelled` records explicit user/operator cancellation transitions
- `task_paused` / `task_resumed` are reserved canonical event types for generic non-HITL pause/resume flows (for example, future budget or operator-driven pauses)
- for HITL flows, the audit trail should show both the pause request (`task_approval_requested` / `task_input_requested`) and the human response (`task_approved` / `task_rejected` / `task_input_received`)
- because HITL resume is stateless, the subsequent `task_claimed` event after a human response is the observable resume point in the lifecycle timeline

---

## 6. LLM Credential Model and Secret Management

> **Phase 2 scope note:** The secret management hardening described in this section (Secrets Manager migration, `provider_credentials` / `tool_credentials` registry tables, shared secret resolver) has been deferred to Phase 3+. Phase 2 retains the Phase 1 credential model (`provider_keys` with plaintext keys, built-in tools using env vars). See [Phase 3+ design-notes.md, Section 8](../phase-3-plus/design-notes.md) for the deferred design. The design below is preserved as the reference specification for when this work is picked up.

### Platform-owns-keys (decided)

The platform holds all LLM provider API keys centrally. Users never provide their own provider credentials. The platform bills users based on per-call cost data captured by Langfuse (self-hosted LLM observability) and the pricing registry in the `models` database table, enforced through the budget model in Section 2.

This decision aligns with the cost-aware scheduling design: the platform must control LLM spending to enforce `budget_max_per_task` and `budget_max_per_hour`. Platform-owned keys also enable centralized rate-limit management and negotiated enterprise pricing with providers.

**BYOK (Bring Your Own Key) is explicitly deferred to Phase 3+.** See [design-notes.md](../phase-3-plus/design-notes.md).

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

The API Service and Console continue reading model availability from `models`. The writer evolves first; the Worker secret-resolution path hardens in Phase 2 as described below.

### Secret management hardening (Phase 2)

Phase 1 stores API keys in the `provider_keys` database table (plaintext, acceptable for local development and early production).

Phase 2 hardens this by migrating to AWS Secrets Manager:

- raw provider and tool secrets live in AWS Secrets Manager, not in PostgreSQL
- the database stores secret references and configuration metadata, not plaintext secret values
- Workers resolve the required secret at point of use from Secrets Manager instead of reading plaintext from `provider_keys`
- Model Discovery resolves provider secrets from the same mechanism and writes only model availability/pricing to `models`
- rotation happens without restarting any services
- least-privilege IAM scoping applies to API Service, Worker Service, Model Discovery, and customer tool runtimes

Even after migration to Secrets Manager, secrets remain operational configuration, not business data. They must never be stored in `tasks`, `checkpoints`, or `task_events`.

### Phase 2+ idea: unified secret reference model

Phase 2 should move away from treating LLM provider keys as a special case. The same control-plane pattern should work for:

- platform-owned LLM provider credentials
- built-in tool credentials (for example `web_search`)
- customer-provided MCP tool runtime credentials
- future BYOK tenant credentials

The key rule is:

- **raw secrets stay in Secrets Manager**
- **PostgreSQL stores only references, scope, policy, and audit metadata**

#### Proposed entities

Provider credential registry:

```
provider_credentials
  provider_id:          text primary key
  secret_ref:           text not null         -- Secrets Manager ARN/name
  credential_scope:     enum(platform)
  status:               enum(active | disabled | invalid)
  last_validated_at:    timestamptz
  last_rotated_at:      timestamptz
  metadata:             jsonb                 -- optional provider-specific info
```

Tool credential registry:

```
tool_credentials
  credential_id:        uuid primary key
  tenant_id:            text not null
  worker_pool_id:       text not null         -- "shared" for built-ins, custom pool for BYOT
  tool_name:            text not null
  secret_ref:           text not null         -- Secrets Manager ARN/name
  exposure_mode:        enum(env | file)      -- how runtime injects it to the MCP server
  status:               enum(active | disabled | invalid)
  created_at:           timestamptz
  updated_at:           timestamptz
  metadata:             jsonb                 -- endpoint allowlist, alias names, etc.
```

These tables are not business data stores for secrets. They are registries telling the runtime:

- which secret to load
- who may use it
- where it may be injected
- whether it is currently valid

#### Runtime resolution path

Instead of loading raw keys from PostgreSQL, the runtime uses a shared secret resolver:

1. Task execution determines which provider/tool credential is needed
2. Worker looks up the reference row in PostgreSQL
3. Worker or tool runtime fetches the raw secret from Secrets Manager using the stored `secret_ref`
4. Secret is held in memory briefly, used for the outbound call, then discarded
5. Secret is never written to checkpoints, task rows, event history, or logs

This gives one consistent pattern for both LLM and tool credentials.

#### Why this is better than storing raw keys in PostgreSQL

- PostgreSQL remains the control plane, not the secret vault
- IAM can scope which runtime may read which secret
- key rotation no longer requires rewriting plaintext values in application tables
- tool credentials and model credentials follow the same operational model
- future BYOK support can reuse the same registry/resolver path without redesigning the worker

#### Service responsibilities

- **Model Discovery:** loads provider secrets via the resolver, validates them, discovers models, updates `models`, and updates credential status/validation timestamps
- **Worker Service:** resolves provider credentials only when instantiating the selected model
- **Built-in tools:** resolve tool credentials from the registry/resolver path instead of reading worker-wide env vars directly
- **Custom Tool Runtime (BYOT):** receives only the secrets explicitly registered for that tool/runtime, not the Worker's full credential set

#### Injection model for tools

Built-in tools and customer MCP servers should not get a broad bag of env vars from the Worker process. Instead:

- the Worker resolves only the credentials required for the invoked tool
- credentials are injected into the built-in tool handler or MCP runtime in the narrowest supported form (`env` or mounted file)
- unrelated credentials are never exposed to the tool process

This is especially important once customer-provided MCP servers exist, because it reduces blast radius if a tool is compromised.

#### Compatibility / migration path

- **Phase 1:** `provider_keys` contains plaintext provider keys; built-in tools like `web_search` still read env vars
- **Phase 2 initial hardening:** introduce resolver + Secrets Manager-backed provider credentials; keep `models` table unchanged
- **Phase 2 extension:** move built-in tool credentials onto the same registry/resolver path
- **Phase 3+:** add tenant-scoped secret ownership and BYOK on top of the same abstraction

The important architectural boundary is that `models` remains the availability/pricing catalog, while credential storage and credential resolution move behind a separate secret-management layer.

---

## 7. Human-in-the-Loop Input

Phase 2 adds two distinct human-in-the-loop mechanisms, both built on LangGraph's `interrupt()` primitive.

### Approval gates (`waiting_for_approval`)

When a non-idempotent tool is about to execute, the graph executor pauses the task and transitions it to `waiting_for_approval`. A human reviews the pending action and either approves or rejects it via the API.

- `POST /v1/tasks/{id}/approve` — resumes execution, the tool call proceeds
- `POST /v1/tasks/{id}/reject` — resumes execution with the rejection reason injected as a tool error, allowing the agent to adjust its plan

### Freeform input (`waiting_for_input`)

When the agent determines it needs clarification or additional information from the user, it can invoke a built-in `request_human_input` tool. This pauses the task and transitions it to `waiting_for_input`, surfacing a prompt message to the user.

- `POST /v1/tasks/{id}/respond` — accepts `{ "message": "..." }`, injects the human response into the conversation as a `HumanMessage`, and resumes graph execution
- The prompt message is stored on the task record (e.g., `pending_input_prompt`) so the Console and API can display what the agent is asking

### Shared semantics

- Both `waiting_for_approval` and `waiting_for_input` are durable pause states — the checkpoint is persisted before pausing and the task releases its lease while waiting for human action
- Resuming a paused task is stateless: the API persists the human response, transitions the task back to `queued`, and any available worker can claim it and continue from the checkpoint with `Command(resume=...)`
- entering a waiting state should clear `lease_owner` and `lease_expiry` so the original worker is free to shut down, deploy, or claim other tasks
- approve/reject/respond should reuse the existing queue wake-up mechanism (`pg_notify('new_task', worker_pool_id)`) rather than introducing a worker-specific resume channel
- the persisted human response should use a documented envelope so the resumed worker can decode the interrupt result deterministically; for example:
  - approval accepted: `{ "kind": "approval", "approved": true }`
  - approval rejected: `{ "kind": "approval", "approved": false, "reason": "..." }`
  - input supplied: `{ "kind": "input", "message": "..." }`
- resuming on a different worker than the one that originally paused the task is expected behavior, not an error case
- Tasks in either state do not count against the agent's `max_concurrent_tasks` limit (they are not consuming compute)
- A configurable timeout (default: 24 hours) auto-transitions unanswered tasks to `dead_letter` with reason `human_input_timeout`
- The Console UI surfaces pending approval/input tasks in a dedicated queue with the agent's prompt and action context

---

## 8. Reliability Additions

Phase 2 extends the Phase 1 recovery model with:

- cost runaway prevention through budget enforcement
- step-history retention and archival policy
- explicit pause/resume behavior
- safer handling for mutable tools and mid-node cancellation

---

## References

- [docs/product-specs/index.md](../../product-specs/index.md) — phase definitions and scope
- [design.md](../phase-1/design.md) — foundation this phase builds on
- [design-notes.md](../phase-3-plus/design-notes.md) — later-phase reference material
