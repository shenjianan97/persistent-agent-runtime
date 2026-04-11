# Design Notes — Phase 3+ Reference Material

**Origin:** Extracted from the former shared design notes. These sections are intentionally beyond Phase 2 and are kept as reference material for later evolution once the Phase 2 design is implemented.

---

## 1. Scaling Analysis

### Back-of-the-envelope numbers

**Assumptions:** Each agent has 1 active task, 20% of agents executing at any moment, ~10 steps/task, ~5s per step (LLM latency dominates), ~5KB per checkpoint.

| Scale | Active | Steps/sec | DB ops/sec | LLM calls/sec | First Bottleneck |
|-------|--------|-----------|------------|----------------|------------------|
| 1K agents | 200 | 40 | 160 | 40 | Nothing — system is idle |
| 10K agents | 2,000 | 400 | 1,600 | 400 | **LLM API rate limits** (most providers cap at 100-500 req/sec) |
| 50K agents | 10,000 | 2,000 | 8,000 | 2,000 | **LLM API cost** ($72K/hour at $0.01/call) |
| 100K agents | 20,000 | 4,000 | 16,000 | 4,000 | **Step history storage** (500GB/day) |

**Key insight:** The runtime is not the first scaling bottleneck. LLM API rate limits and cost bind before the control plane does. This validates prioritizing cost-aware scheduling before doing a major storage/queue redesign.

### Storage retention

At 10K agents with moderate activity: ~50GB/day of step history. Retention policy: keep full step history for 7 days, archive to S3 after 7 days, delete after 90 days. This keeps the hot store under 350GB.

---

## 2. Queue / Storage Evolution Options

These are later-phase structural options if PostgreSQL becomes the bottleneck:

- transactional outbox plus SQS FIFO for queueing
- worker-level liveness or alternate lease models to reduce heartbeat write load
- storage-tier changes driven by retention and write amplification

These are deliberately deferred until scale, cost, or operational data shows the current design is no longer sufficient.

---

## 3. DynamoDB Single-Table Design

Potential redesign if PostgreSQL scaling limits are hit:

```
PK                          SK                              Entity
AGENT#agent123             METADATA                         Agent config
AGENT#agent123             TASK#task456                     Task record
AGENT#agent123             TASK#task456#STEP#001            Step record
AGENT#agent123             TASK#task456#STEP#002            Step record

GSI1: status (PK) + created_at (SK)     — worker polling for queued tasks
GSI2: lease_expiry (PK)                 — reaper scanning for expired leases
```

This is a future alternative, not a committed Phase 2 direction.

---

## 4. Future Tool Integration

These ideas are later than the core Phase 2 BYOT runtime:

- **OpenAPI auto-wrapping:** accept an OpenAPI spec and auto-generate an MCP server from it
- **A2A (Agent-to-Agent) protocol:** enable cross-agent tool invocation via Google's A2A protocol for multi-agent coordination scenarios

---

## 5. Bring Your Own Key (BYOK)

Phase 2 uses platform-owned LLM API keys (see [design.md, Section 6](../phase-2/design.md)). If customer demand arises for users to bring their own provider API keys, this would require:

- per-tenant credential vaults in Secrets Manager
- per-agent API key overrides in the Agent entity
- provider-specific rate-limit isolation (each tenant's key has its own rate limits)
- billing model changes (platform no longer bears LLM cost for BYOK tenants)
- `create_llm()` in the worker accepting an optional `api_key` parameter override

This is feasible on top of the dynamic provider registry architecture but adds significant complexity to secret management and billing.

---

## 6. Execution Sandbox

Phase 2 BYOT isolates customer-provided MCP servers in separate ECS tasks with network restrictions. Phase 3+ extends this with deeper sandboxing for all tool execution, particularly important if the platform adds a `code_interpreter` tool or allows agents to run LLM-generated code.

### Threat model

| Threat | Example | Current mitigation |
|--------|---------|-------------------|
| Code escape to host | LLM-generated code accesses host filesystem or network | None — Phase 1 tools run in-process |
| Secret exfiltration | Compromised tool reads worker's DB credentials or API keys | None — co-located tools share the worker's process memory |
| Resource exhaustion | Runaway tool consumes unbounded CPU/memory/time | None — no per-tool resource limits |
| Lateral movement | Tool accesses other tenants' containers or internal services | Phase 2 BYOT network policy (customer tools only) |

### Sandbox layers

**Process-level isolation:** Run each tool invocation in a sandboxed process (e.g., gVisor, Firecracker microVM, or at minimum a restricted container) with no access to the worker's filesystem or memory. The worker communicates with the sandbox over a local socket or stdio pipe using the MCP protocol.

**Network policy per tool:** Each tool execution gets a network policy specifying exactly which external endpoints it may reach. Built-in tools like `web_search` get outbound HTTPS only. `calculator` gets no network access. Customer tools get their registered endpoint allowlist.

**Resource quotas:** Per-tool-invocation limits on CPU time, memory, and wall-clock duration. Exceeded limits kill the sandbox and return a structured error to the graph executor for retry/dead-letter classification.

**Filesystem restrictions:** Tool sandboxes get an ephemeral scratch directory only. No access to the worker's checkpoint data, configuration, or credentials. Credentials needed by the tool (e.g., API keys) are injected as environment variables into the sandbox, scoped to the minimum required.

### Migration path

- **Phase 2:** BYOT isolation (already designed) covers customer tools. Built-in tools remain co-located but are limited to the read-only set (`web_search`, `read_url`, `calculator`).
- **Phase 3:** Move built-in tools into sandboxed execution. Add `code_interpreter` tool with full sandbox isolation. This is a prerequisite for any tool that executes LLM-generated code.
- **Phase 3+:** Per-tenant sandbox policies, audit logging of sandbox escapes, and integration with cloud-native security tooling (e.g., AWS Nitro Enclaves for sensitive workloads).

---

## 7. Orchestrator Component Rewrites (Poller, Heartbeat, Reaper)

Currently, the entire worker service (orchestrator + LangGraph executor) is written in Python to maintain architectural simplicity and allow AI agents (like Claude) to easily reason about the entire codebase. 

If performance, concurrency, or footprint becomes a significant issue at scale, Phase 3+ could consider:
- Rewriting the **Poller**, **Heartbeat**, and **Reaper** components in **Go** or **Java** for true multi-threading and lower resource overhead.
- Keeping the LangGraph **Executor** in Python.
- Establishing an IPC or local RPC layer between the Go/Java orchestrator and the Python executor.

This is a heavy architectural shift and should only be pursued if the Python `asyncio` event loop becomes a demonstrable bottleneck.

---

## 8. Secret Management Hardening

This work was originally part of Phase 2 Track 4 ("Secrets and Tool Runtime Foundation") but was deliberately deferred when Track 4 was rescoped to focus on BYOT (custom MCP tool server integration). The full design specification is preserved in [Phase 2 design.md, Section 6](../phase-2/design.md).

### Problem

Phase 1 stores LLM provider API keys as plaintext in the `provider_keys` PostgreSQL table. Built-in tool credentials (e.g., `TAVILY_API_KEY` for `web_search`) are read from worker process environment variables. This is acceptable for local development but inadequate for production:

- plaintext secrets in the database increase blast radius if the DB is compromised
- environment variables give every tool access to every credential in the worker process
- key rotation requires redeploying services
- no audit trail for credential access

### Planned approach

1. **Migrate provider credentials from plaintext to AWS Secrets Manager references.** Introduce a `provider_credentials` registry table that stores `secret_ref` (Secrets Manager ARN) instead of raw `api_key`. The `provider_keys` table is kept read-only during transition and dropped after verification.

2. **Introduce a `tool_credentials` registry table** for tool-specific credentials (built-in tools and future BYOT tool servers). Each row maps `(tenant_id, worker_pool_id, tool_name)` to a `secret_ref` with an `exposure_mode` (env or file).

3. **Shared `SecretResolver` abstraction.** A single Python interface used by model discovery, workers, and built-in tools to resolve credentials:
   - `SecretsManagerResolver` — production path, fetches from Secrets Manager with in-memory caching (TTL ~300s)
   - `EnvVarResolver` — local development fallback
   - `ChainResolver` — tries Secrets Manager first, falls back to env vars

4. **Per-tool credential injection.** When invoking a tool, the worker resolves only the credentials registered for that specific tool. Unrelated credentials are never exposed. This is especially important for customer-provided MCP servers (BYOT), where the tool process must not see the platform's LLM API keys.

5. **Migration path:**
   - Phase 1 (current): `provider_keys` plaintext + env vars
   - Phase 3 initial: introduce resolver + Secrets Manager-backed `provider_credentials`; `models` table FK migrates from `provider_keys` to `provider_credentials`
   - Phase 3 extension: built-in tool credentials and BYOT tool server credentials on the same registry/resolver path
   - Phase 3+: tenant-scoped secret ownership for BYOK support (see Section 5)

6. **`tool_servers.auth_token` migration.** Phase 2 Track 4 stores MCP server auth tokens as plaintext in the `tool_servers` table. When this hardening work is implemented, `auth_token` should migrate to a `secret_ref` pointing to Secrets Manager.

### Key entities (from Phase 2 design.md Section 6)

```
provider_credentials
  provider_id:          text primary key
  secret_ref:           text not null         -- Secrets Manager ARN/name
  credential_scope:     enum(platform)
  status:               enum(active | disabled | invalid)
  last_validated_at:    timestamptz
  last_rotated_at:      timestamptz
  metadata:             jsonb

tool_credentials
  credential_id:        uuid primary key
  tenant_id:            text not null
  worker_pool_id:       text not null
  tool_name:            text not null
  secret_ref:           text not null
  exposure_mode:        enum(env | file)
  status:               enum(active | disabled | invalid)
  created_at:           timestamptz
  updated_at:           timestamptz
  metadata:             jsonb
```

### Trigger conditions

This work should be prioritized when:
- the platform is deployed to a shared or production environment where plaintext DB secrets are a compliance concern
- BYOT adoption increases and credential isolation between tool servers becomes operationally important
- key rotation without service restart is required

---

## 9. Non-Idempotent Tool Safeguards

Deferred from Phase 2 Track 5 (originally scoped as "Memory and Tool Safety Features", now renamed to just "Memory"). The original design lives in [Phase 2 design.md, Section 4 — Non-idempotent tool safety](../phase-2/design.md).

### Problem

Phase 1 avoids mutable tools entirely. As the platform supports custom tools (BYOT) and sandbox-based execution, some tools will have side effects (send email, create resource, charge payment). Blind re-execution after a crash can cause duplicated side effects.

### Planned approach

- **`idempotent: true|false` on tool schema** — each MCP tool declares whether it is safe to re-execute
- **Checkpoint-before-call for mutable tools** — the graph executor persists a LangGraph checkpoint before invoking any tool marked `idempotent: false`, recording that a side effect is about to occur
- **Dead-letter on crash** — if a crash occurs after a non-idempotent tool call started (detected via checkpoint), the task moves to `dead_letter` rather than being automatically retried. A human or operator inspects and decides whether to redrive.
- **Integration with approval gates** — the existing `waiting_for_approval` HITL flow (delivered in Phase 2 Track 2) can be configured to trigger automatically for non-idempotent tools, requiring human sign-off before side effects happen

### Trigger conditions

This work should be prioritized when:
- customers begin registering BYOT tools with real-world side effects (payments, notifications, resource provisioning)
- the platform needs to guarantee at-most-once semantics for mutable operations

---

## 10. Batch, Webhooks, and Structured Output

Deferred from the [agent-capabilities design doc](../agent-capabilities/design.md) to keep initial scope focused on sandbox, artifacts, and file input.

### Batch task API

Atomic submission of multiple tasks in a single request:

- `POST /v1/tasks/batch` — accepts an array of task inputs with shared config
- Each task independently scheduled and executed
- `batch_id` stored on each task row for grouping
- `GET /v1/batches/{batch_id}` — aggregated status (completed/failed/running/queued counts)
- Max 1000 tasks per batch

### Webhooks

Push notifications on task events, registered at the agent level:

- `POST /v1/agents/{id}/webhooks` — register URL, event types, HMAC signing secret
- Events: `task.completed`, `task.failed`, `task.waiting_for_input`, `task.waiting_for_approval`, `batch.completed`
- Delivery: fire-and-forget with retry (3 attempts, exponential backoff)
- Failed deliveries logged but do not affect task status

### Structured output schemas

Allow agents to specify an expected JSON output schema:

- `output_schema` in agent config — JSON Schema definition
- Schema included in agent's system prompt so the LLM knows the expected format
- Worker validates final response against schema
- If invalid: correction prompt sent to LLM (up to 2 retries), then task fails
- Valid output stored in `output.structured` alongside `output.result`

### Trigger conditions

- **Batch:** when customers need to process large volumes of similar tasks (document processing, data extraction)
- **Webhooks:** when polling becomes impractical (high task volume, downstream automation)
- **Structured output:** when customers need machine-readable results for pipeline integration
