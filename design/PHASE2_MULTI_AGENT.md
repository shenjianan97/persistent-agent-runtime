# Phase 2 Design — Multi-Agent & Cost-Aware Scheduling

**Status:** Not started. This document is a placeholder outlining Phase 2 scope based on [PROJECT.md](../PROJECT.md).

**Goal:** Support multiple agents with fair scheduling and budget enforcement.

---

## Scope

- Agent as first-class entity (own table, replaces inline `agent_config_snapshot`)
- Per-agent concurrency limits and configuration management
- Cost-aware scheduler: per-agent budgets, tasks paused (not failed) when budget exceeded
- Fair scheduling: weighted fair queuing to prevent agent monopolization
- Worker backpressure: pull-based concurrency semaphore
- Long-term memory: append-only S3 entries with LLM-based compaction
- Custom Tool Runtime (BYOT): customer-provided MCP servers running in platform-managed isolated containers
- `waiting_for_approval` task status for human-in-the-loop workflows
- Non-idempotent tool guards: `idempotent: true|false` annotation on MCP tool schema, checkpoint-before-call for mutable tools, dead-letter on re-execution after crash (deferred from Phase 1 — Phase 1 only allows idempotent read-only tools via co-located MCP server)
- Redrive checkpoint rollback: `rollback_last_checkpoint` option on `POST /redrive` to delete latest checkpoint before requeue (deferred from Phase 1 — not needed with idempotent-only tools)
- Mid-node task cancellation: asyncio task cancellation during in-flight LLM calls (deferred from Phase 1 — Phase 1 uses between-node cancellation only)
- SQS FIFO migration via transactional outbox (if throughput demands it)
- DynamoDB single-table design (if PostgreSQL scaling limits are hit)

---

## Key Design Areas (To Be Detailed)

### Agent Entity
- Promotion from inline config to its own table
- Agent CRUD API
- Per-agent budget tracking (hourly and per-task)

### Cost-Aware Scheduling
- Budget enforcement: pause vs fail semantics
- Cost aggregation across steps and tasks
- Budget increase API for paused tasks

### Memory Compaction
- Append-only S3 entry model (already designed in DESIGN_NOTES_PHASE2.md Section 3)
- Compaction triggers: user-initiated and periodic
- Compaction as a runtime task (gets durability guarantees)

### Custom Tool Runtime (BYOT — Bring Your Own Tools)

Replaces the former "Private Workers (BYOW)" concept. Instead of customers deploying a full Worker Service, customers provide only their tool code as an MCP server container. The platform runs it.

- **Architecture:** Customer uploads an MCP server container image (or code bundle). The platform deploys it as an isolated ECS task within the platform's VPC, co-located with the Worker Service. Tool calls flow over private networking (VPC-internal) — nothing is exposed to the internet.
- **Tool registration API:** `POST /v1/tool-runtimes` registers a customer's MCP server (container image URI, resource limits, credentials). The platform provisions an isolated container and makes its tools available via `listTools`.
- **Routing:** `worker_pool_id` on tasks becomes a tool runtime routing key. When the Worker Service executes a tool call, it routes to the appropriate MCP server based on the task's tool runtime configuration.
- **Security model:** Customer MCP servers run in isolated containers with no DB access, no access to other customers' containers, and no public internet exposure. VPC security groups restrict traffic to Worker Service → MCP server only.
- **Non-idempotent tool safety:** The control plane (Worker Service) checkpoints before calling mutable tools. If a crash occurs mid-tool-call and re-execution is needed, the control plane can detect this and dead-letter the task instead of blindly re-executing a non-idempotent tool.
- **Unlocks safe code execution:** Permits agents to run `bash` and `python_execute` tools safely by isolating side-effects within the customer's container, completely sandboxed from the control plane and other customers.
- **Customer simplicity:** Customers only need to implement MCP tool handlers (simple request/response). They don't need to understand durable execution, checkpointing, leases, or the DB schema.

### Future Tool Integration (Phase 3+)
- **OpenAPI auto-wrapping:** Accept an OpenAPI spec and auto-generate an MCP server from it, reducing customer onboarding friction for existing REST APIs
- **A2A (Agent-to-Agent) protocol:** Enable cross-agent tool invocation via Google's A2A protocol for multi-agent coordination scenarios

---

## References

- [PROJECT.md](../PROJECT.md) — Phase 2 scope definition
- [DESIGN_NOTES_PHASE2.md](./DESIGN_NOTES_PHASE2.md) — Phase 2+ reference material
- [PHASE1_DURABLE_EXECUTION.md](./PHASE1_DURABLE_EXECUTION.md) — Foundation this phase builds on
