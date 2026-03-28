# Phase 1 Implementation Progress

This document tracks the execution status of the Agent Tasks defined in the Implementation Plan.

| Task | Component | Status | Description |
|------|-----------|--------|-------------|
| [Task 1](./agent_tasks/task-1-database-schema.md) | Database Schema | Done | Added the Phase 1 Postgres schema, schema README, and Docker-backed verification harness for queue and checkpoint query flows. |
| [Task 2](./agent_tasks/task-2-api-service.md) | API Service | Done | Spring Boot REST API with 7 endpoints (submit, status, checkpoints, cancel, dead-letter, redrive, health), full validation, pg_notify, 36 tests. |
| [Task 3](./agent_tasks/task-3-worker-service-core.md) | Worker Service Core | Done | Task poller (FOR UPDATE SKIP LOCKED + LISTEN/NOTIFY), heartbeat manager, distributed reaper, structured logging, 95 tests. |
| [Task 4](./agent_tasks/task-4-langgraph-checkpointer.md) | LangGraph Checkpointer | Done | Added a lease-aware `PostgresDurableCheckpointer`, public package exports, and unit/integration coverage for checkpoint writes, reads, and lease revocation behavior. |
| [Task 5](./agent_tasks/task-5-mcp-server.md) | Co-located MCP Server | Done | Added a FastMCP-based in-process tool server exposing `web_search`, `read_url`, and `calculator`, plus worker-service documentation and test coverage. |
| [Task 6](./agent_tasks/task-6-graph-executor.md) | Graph Executor | Done | Graph assembly, failure classification, retryable/non-retryable handling, cost tracking, unit/integration testing. |
| [Task 7](./agent_tasks/task-7-console.md) | Console | Done | Dashboard, task list, task dispatcher, execution telemetry, dead letter queue. Brutalist dark-mode UI with IBM Plex Mono + Syne fonts. |
| [Task 8](./agent_tasks/task-8-aws-infrastructure.md) | AWS Cloud Infrastructure | Done | Added CDK network/data/compute stacks, service container packaging, schema bootstrap + model discovery deployment hooks, and infrastructure deployment docs. |
| [Task 9](./agent_tasks/task-9-langfuse-observability.md) | Langfuse Integration & Observability Split | In Progress | Local Langfuse observability is implemented and verified across Worker, API, and Console. Current local follow-up tracking lives in [task-9-local-followup.md](./task-9-local-followup.md), while the original Task 9 spec remains unchanged. AWS/CDK deployment, CloudWatch operator observability, and destructive checkpoint-column cleanup are deferred follow-up work. |

## Notes
- Task 1 must be completed before downstream components that rely on the schema can be fully tested.
- Tasks 2, 3, 4 can be worked on in parallel after Task 1 is defined.
- Tasks 5 and 7 have no dependencies and can start immediately alongside Task 1.
- Task 6 depends on 3, 4, and 5.
- Task 7 (Demo Dashboard) depends on Task 2 (API Service) for endpoint consumption.
- Task 8 can be worked on in parallel with all other tasks but is required for cloud deployment.
- Task 9 depends on all Tasks 1–8 being complete. It modifies Worker (Task 6), API (Task 2), Console (Task 7), and Infrastructure (Task 8).

## Task 8 — CDK Test Fix (Post-Completion)
- **Issue:** `npm test` in `infrastructure/cdk/` failed during synth-time with `CannotFindFile: api-image.Dockerfile`.
- **Root cause:** Stale compiled `.js` files (`compute-stack.js`, `data-stack.js`, `network-stack.js`) in `infrastructure/cdk/lib/` shadowed the TypeScript sources during ts-jest module resolution. These artifacts were from an earlier iteration that used different Dockerfile paths and lacked `unitTestMode` support. Node resolved `.js` over `.ts`, bypassing the test-mode Docker image stubs.
- **Fix:** Deleted the three stale `.js` files (already covered by `.gitignore` pattern `infrastructure/cdk/lib/*.js`). No source code changes needed.
- **Prevention:** The `.gitignore` already excludes compiled output from `lib/`. If stale artifacts reappear locally, run `rm infrastructure/cdk/lib/*.js` before testing.
- **Verified:** `cd infrastructure/cdk && npm run build` passes; `cd infrastructure/cdk && npm test -- --runInBand` passes (4/4 tests, all 3 stacks synthesize cleanly).
