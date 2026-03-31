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

Phase 2 uses platform-owned LLM API keys (see [PHASE2_MULTI_AGENT.md, Section 6](../phase-2/PHASE2_MULTI_AGENT.md)). If customer demand arises for users to bring their own provider API keys, this would require:

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
