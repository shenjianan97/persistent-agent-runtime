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
