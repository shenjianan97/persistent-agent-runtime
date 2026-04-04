# Phase 2 Track 1 — Agent Control Plane: Orchestrator Plan

## A1. Implementation Overview

Track 1 establishes Agent as a first-class entity in the Persistent Agent Runtime. This requires:

1. A new `agents` database table with composite PK `(tenant_id, agent_id)`
2. A new Agent CRUD API at `/v1/agents` (POST, GET, GET/{id}, PUT)
3. A refactored task submission flow that resolves agent configuration from the stored agent record
4. Enriched task responses with `agent_display_name` sourced from per-task snapshots
5. New Console pages for agent management (list + detail)
6. An updated task submission UX (agent selector replaces inline config)
7. Updated integration tests for the new submission contract

The worker service requires minimal changes — the claim query's `RETURNING t.*` already returns all columns, so the new `agent_display_name_snapshot` column is automatically included. The executor only reads `agent_config_snapshot`, which continues to be populated via snapshot-at-submission.

**Canonical design contract:** `docs/design-docs/phase-2/track-1-agent-control-plane.md`

---

## A2. Impacted Components

| Component | Path | Change Type | Description |
|-----------|------|-------------|-------------|
| Database Schema | `infrastructure/database/migrations/` | new migration | `0005_agents_table.sql`: agents table, tasks column, FK |
| Agent CRUD API | `services/api-service/` | new code | AgentController, AgentService, AgentRepository |
| Task Submission | `services/api-service/` | modification | Remove inline agent_config, resolve from agents table |
| Task Responses | `services/api-service/` | modification | Add agent_display_name to all task-facing responses |
| Console: Agents | `services/console/src/features/agents/` | new code | Agents list + detail pages, create dialog |
| Console: Submit + Views | `services/console/src/features/submit/` | modification | Agent selector, task view enrichment |
| Worker Service | `services/worker-service/` | minimal | Test fixtures only (FK compliance) |
| Integration Tests | `tests/backend-integration/` | modification | Agent-based submission, CRUD tests |

---

## A3. Dependency Graph

```
Task 1 (Schema) ─┬──→ Task 2 (Agent CRUD) ──→ Task 3 (Submission Refactor) ──┐
                  │                         ╲                                  │
                  │                          ╲──→ Task 5 (Console: Agents) ────┤
                  │                                                            ├──→ Task 6 (Console: Submit + Views)
                  └──→ Task 4 (Response Enrichment) ──────────────────────────┘
                  │
                  └──→ Task 7 (Integration Tests) ←── Tasks 2, 3, 4
```

**Parallelization opportunities:**
- After Task 1: Tasks 2 and 4 can run in parallel
- After Task 2: Tasks 3 and 5 can run in parallel
- Task 7 waits for all backend tasks (1-4)

---

## A4. Data / API / Schema Changes

**New `agents` table:** Additive migration. No backward compatibility concern.

**Task submission contract change:** Breaking change. `agent_config` is removed from `POST /v1/tasks`. Callers must create agents first, then reference by `agent_id`. The `test_seed.sql` must seed agents before integration tests submit tasks.

**`tasks.agent_display_name_snapshot`:** Nullable TEXT column. Existing tasks have NULL — backward compatible.

**FK constraint:** `tasks(tenant_id, agent_id)` → `agents(tenant_id, agent_id)`. The system is still in development and the design doc states "does not need to preserve existing development data." The migration assumes a clean database — no data backfill is needed.

---

## A4.1. Task Handoff Outputs

| Task | Output |
|------|--------|
| Task 1 | Migration `0005_agents_table.sql` (agents table, tasks column, FK), updated `test_seed.sql` with seed agent for E2E tests |
| Task 2 | `AgentController`, `AgentService`, `AgentRepository` with full CRUD. Shared `ConfigValidationHelper` |
| Task 3 | Refactored `TaskSubmissionRequest` (no agentConfig), atomic `INSERT...SELECT` in `TaskRepository.insertTaskFromAgent()` that resolves agent and inserts task in one SQL statement |
| Task 4 | Updated response records with `agent_display_name` field, updated repository queries |
| Task 5 | Working Agents list and detail pages with create dialog, edit form, status toggle, "Submit Task" CTA |
| Task 6 | Reworked submit page with agent selector; task views showing display names with agent links |
| Task 7 | Updated integration test suite, agent CRUD tests, worker FK compatibility |

---

## A5. Integration Points

| Caller | Callee | Interface Change | Failure Handling |
|--------|--------|-------------------|-----------------|
| API Service | PostgreSQL `agents` | New CRUD queries; task insertion joins agent lookup + snapshot in same transaction | 404 missing agent, 400 disabled agent, 409 duplicate agent_id |
| Console | API `/v1/agents` | New fetch calls for agent CRUD; submit page fetches agent list + detail | Empty state when no agents; error toast on failures |
| Console | API `/v1/tasks` | Submit payload removes `agent_config`; responses gain `agent_display_name` | Validation errors displayed in form |

---

## A6. Deployment and Rollout

The API and Console must be deployed together because the task submission contract changes. Deploying the API before the Console would break the current Console's inline `agent_config` submission.

**Migration:** `0005_agents_table.sql` is applied automatically by the existing schema-bootstrap handler (CDK Lambda for AWS, `_apply_migrations()` in conftest.py for local E2E tests). It follows the `^\d{4}_.*\.sql$` naming convention and is tracked in the `schema_migrations` ledger. Do not apply it manually — the ledgered bootstrap path handles it.

**For coordinated deployment (recommended for production):**
1. Deploy updated code (migration file, API, Console, worker) together in a single release
2. Schema bootstrap automatically applies `0005_agents_table.sql` via the ledger
3. Create initial agents via the API before submitting tasks

**For local development (acceptable for dev environments):**
1. `make db-reset` or restart services to apply all migrations including `0005`
2. Deploy updated API service with agent CRUD + refactored task submission
3. Deploy updated Console with agents area and reworked submit page
4. Deploy updated worker service (minimal changes — test fixtures only)
5. Create initial agents via the API before task submission

**Seeding for E2E tests:** `test_seed.sql` provides seed agent data for the integration test suite. It is not used in production — the schema-bootstrap handler excludes non-numbered files. Production environments must create agents via the API.

**Note:** This is a breaking change to the task submission contract. Between steps 2 and 3 in the local dev flow, the old Console cannot submit tasks. This is acceptable for development but not for production.

---

## A7. Observability

No new observability requirements for Track 1. Existing structured logging covers task submission. Agent CRUD operations use standard Spring Boot request logging. The `agent_id` already appears in all task log entries.

---

## A8. Risks and Open Questions

| Risk | Mitigation |
|------|-----------|
| Breaking submission contract | API and Console must deploy together for production. For local dev, a brief outage between API and Console deploy is acceptable |
| FK constraint requires clean DB | The system is still in development — the design doc explicitly allows this. Migration adds FK directly; run `make db-reset` for local dev |
| `agent_id` format validation | Path-safe slug regex: `^[a-z0-9][a-z0-9_-]{0,63}$` matching existing usage patterns |

---

## A9. Orchestrator Guidance

- Use `docs/design-docs/phase-2/track-1-agent-control-plane.md` as the canonical design contract
- Task 1 must land first. Tasks 2 and 4 can proceed in parallel after Task 1
- Task 3 depends on Task 2 because it needs `AgentRepository` to resolve agents
- The `LangfuseEndpointController`/`Service`/`Repository` pattern is the direct template for Agent CRUD
- Config validation logic (`validateModel`, `validateAllowedTools`) should be extracted from `TaskService` into a shared utility so both `AgentService` and `TaskService` can call it
- Task submission must atomically resolve the agent, validate the model, and insert the task in a single SQL statement (`INSERT...SELECT FROM agents JOIN models WHERE status = 'active' AND is_active = true`) to prevent TOCTOU races from concurrent agent/model updates. The current codebase has no `@Transactional` annotations, so SQL-level atomicity is the preferred approach
- Agent config defaults (temperature, allowed_tools) must be applied at agent creation/update time, not at task submission time, because the atomic INSERT snapshots config directly from the agents table
- Do not introduce agent statuses beyond `active` and `disabled`
- Do not add DELETE endpoints

---

## B. Agent Task Files

| Task | File | Description |
|------|------|-------------|
| Task 1 | [task-1-database-schema.md](agent_tasks/task-1-database-schema.md) | Agents table, tasks column, FK constraint, seed data |
| Task 2 | [task-2-agent-crud-api.md](agent_tasks/task-2-agent-crud-api.md) | AgentController/Service/Repository, POST/GET/GET/{id}/PUT |
| Task 3 | [task-3-task-submission-refactor.md](agent_tasks/task-3-task-submission-refactor.md) | Remove inline agent_config, resolve from agents table |
| Task 4 | [task-4-task-response-enrichment.md](agent_tasks/task-4-task-response-enrichment.md) | Add agent_display_name to all task responses |
| Task 5 | [task-5-console-agents-area.md](agent_tasks/task-5-console-agents-area.md) | Agents list + detail pages, sidebar nav, create dialog |
| Task 6 | [task-6-console-submit-task-views.md](agent_tasks/task-6-console-submit-task-views.md) | Agent selector submit page, display_name in task views |
| Task 7 | [task-7-integration-tests.md](agent_tasks/task-7-integration-tests.md) | Updated tests, agent CRUD tests, worker FK compat |
