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
- Private Workers (BYOW): task routing to customer-deployed workers via `worker_pool_id`
- `waiting_for_approval` task status for human-in-the-loop workflows
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

### Private Workers (BYOW)
- Worker pool registration and routing
- Security model: workers pull tasks, no inbound ports needed
- MCP server access from customer VPC

---

## References

- [PROJECT.md](../PROJECT.md) — Phase 2 scope definition
- [DESIGN_NOTES_PHASE2.md](./DESIGN_NOTES_PHASE2.md) — Phase 2+ reference material
- [PHASE1_DURABLE_EXECUTION.md](./PHASE1_DURABLE_EXECUTION.md) — Foundation this phase builds on
