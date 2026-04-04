# Langfuse Customer Integration — Orchestrator Plan

**Role**: You are the Orchestrator Agent responsible for overseeing the refactoring of Langfuse from platform-owned infrastructure to a customer-owned integration in the Persistent Agent Runtime.

**CRITICAL PRE-WORK:** Before delegating any tasks or making technical decisions, you MUST read the following context files:
1. `docs/design-docs/langfuse/design.md`
2. `docs/design-docs/phase-1/design.md`

Your responsibilities are to assign the individual tasks listed in Section B to specialized coding agents, track their progress, and resolve any dependencies or blockers.

---

### SECTION A — Implementation Plan

#### A1. Implementation Overview

The current Langfuse integration treats Langfuse as platform-owned infrastructure: a single instance started via `make start`, credentials in environment variables, worker fails without it, and the Console queries it to render execution traces. This refactoring decouples Langfuse entirely, making it an opt-in, per-customer integration where:

- Customers register their Langfuse endpoints via a Settings page
- Tasks optionally reference a registered endpoint
- Workers resolve credentials per-task and publish traces only when configured
- The platform tracks cost/tokens internally via checkpoint data, independent of Langfuse
- The Console shows only platform-owned data; customers view traces in their own Langfuse UI

#### A2. Impacted Components / Modules

  Component: Database Schema
  Change type: modification of existing schema
  Path: `infrastructure/database/migrations/0001_phase1_durable_execution.sql`, `infrastructure/database/migrations/test_seed.sql`
  Description: Add `langfuse_endpoints` table and `langfuse_endpoint_id` FK column on `tasks` directly to the base schema. Add local dev seed. No new migration file — system is in development with no production data. Requires `make db-down && make db-up`.

  Component: API Service — Endpoint Management
  Change type: new code + modification
  Path: `services/api-service/src/main/java/com/persistentagent/api/`
  Description: New CRUD REST resource `/v1/langfuse-endpoints` with repository, service, and controller. Task submission gains optional `langfuse_endpoint_id`. Task queries include the new column.

  Component: Worker Service
  Change type: modification
  Path: `services/worker-service/`
  Description: Remove global Langfuse configuration and startup assertion. Add per-task credential resolution from `langfuse_endpoints` table. Restore internal cost/token tracking to checkpoint columns. All Langfuse operations are gracefully degraded — failures never fail a task.

  Component: API Service — Observability
  Change type: removal + modification
  Path: `services/api-service/src/main/java/com/persistentagent/api/service/observability/`
  Description: Remove `LangfuseTaskObservabilityService` (queries customer's Langfuse instance — architecturally wrong). Cost/token data now sourced from checkpoint aggregation in the platform DB. Simplify observability endpoint to return platform-owned data only.

  Component: Console Frontend
  Change type: new code + modification
  Path: `services/console/src/`
  Description: New Settings/Integrations page for Langfuse endpoint CRUD. Task submission form gains optional endpoint dropdown. ObservabilityTrace and CostSummary reworked to source from platform DB checkpoint data instead of Langfuse API.

  Component: Infrastructure
  Change type: relocation + modification
  Path: `infrastructure/local/langfuse/`, `tests/fixtures/langfuse/`, `Makefile`
  Description: Move Langfuse docker-compose from platform infrastructure to test fixture. Update Makefile targets. Remove Langfuse from `make start`.

#### A3. Dependency Graph

  Task 1 (Database Schema) → depends on no prior tasks
  Task 2 (API CRUD + Task Submission) → depends on → Task 1
  Task 3 (Worker Refactor) → depends on → Task 1
  Task 4 (API Observability Refactor) → depends on → Task 2, Task 3
  Task 5 (Console + Infrastructure) → depends on → Task 2, Task 3, Task 4

Tasks 2 and 3 can execute in parallel after Task 1 completes.

```
Task 1 (DB migration)
  ├──> Task 2 (API CRUD + task submission)
  └──> Task 3 (Worker per-task + cost restore)
           ├──> Task 4 (API observability refactor)
           └──> Task 5 (Console + infrastructure)
```

#### A4. Data / API / Schema Changes

  Change: Add `langfuse_endpoints` table and `langfuse_endpoint_id` on `tasks` to base schema
  Type: schema (modify existing 0001 migration)
  Backward compatible: N/A (development — requires db recreate)

  Change: New REST resource `/v1/langfuse-endpoints`
  Type: API
  Backward compatible: yes (new endpoints only)

  Change: Optional `langfuse_endpoint_id` in task submission
  Type: API
  Backward compatible: yes (optional field)

  Change: Observability endpoint simplified to platform-owned data
  Type: API
  Backward compatible: no (response shape changes — Langfuse spans removed)

  Change: Remove global Langfuse env vars from worker
  Type: configuration
  Backward compatible: no (LANGFUSE_ENABLED, LANGFUSE_HOST, etc. no longer read)

---

### SECTION B — Task List

| Task | File | Description | Dependencies |
|------|------|-------------|-------------|
| 1 | `tasks/task-1-database-schema.md` | Modify 0001 schema: langfuse_endpoints table, tasks column, local dev seed | None |
| 2 | `tasks/task-2-api-crud.md` | Repository, service, controller for endpoint CRUD + task submission wiring | Task 1 |
| 3 | `tasks/task-3-worker-refactor.md` | Remove global Langfuse, per-task resolution, restore cost tracking | Task 1 |
| 4 | `tasks/task-4-api-observability-refactor.md` | Remove Langfuse query code, checkpoint-based cost aggregation | Tasks 2, 3 |
| 5 | `tasks/task-5-console-and-infrastructure.md` | Settings page, task form dropdown, console rework, infra relocation | Tasks 2, 3, 4 |
