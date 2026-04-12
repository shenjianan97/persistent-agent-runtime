# Agent Capabilities — Track 1: Output Artifact Storage

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable agents to produce output files (reports, data, code) that users can list and download — establishing the S3 artifact infrastructure that Track 2 (sandbox & file input) builds on.

**Architecture:** Artifacts stored in S3 (LocalStack for local dev, real AWS in production) with metadata in a `task_artifacts` database table. The worker produces output artifacts via a new `upload_artifact` built-in tool that uploads content to S3 and records metadata in the DB. The API service provides listing and download endpoints. The Console shows an artifacts tab in the task detail view.

**Tech Stack:** PostgreSQL (artifact metadata), S3/LocalStack (blob storage), Spring Boot + AWS SDK v2 (API file handling), Python boto3 (worker S3 client), React/TypeScript (console artifacts tab)

---

## A1. Implementation Overview

Track 1 delivers end-to-end output artifact support:

1. **Database migration** — `task_artifacts` table for artifact metadata
2. **LocalStack Docker setup** — S3 emulation for local development with bucket initialization
3. **Worker S3 client** — Python boto3 wrapper for artifact upload/download
4. **API artifact repository + S3 service** — JDBC queries for `task_artifacts` table and S3 client for file streaming
5. **API artifact endpoints** — List and download artifacts per task
6. **`upload_artifact` built-in tool** — Agent tool for producing output artifacts (no sandbox required)
7. **Console artifacts tab** — Artifact list + download UI in task detail view
8. **Integration tests** — End-to-end output artifact flow validation

No sandbox concepts in this track. Sandbox config, file input, multipart submission, and sandbox tools are all in Track 2.

**Canonical design contract:** `docs/design-docs/agent-capabilities/design.md`

---

## A2. Impacted Components

| Component | Path | Change Type | Description |
|-----------|------|-------------|-------------|
| DB migration | `infrastructure/database/migrations/0009_artifact_storage.sql` | new migration | `task_artifacts` table |
| Docker Compose | `docker-compose.yml` | new file | LocalStack S3 container for local dev |
| Makefile | `Makefile` | modification | LocalStack startup/teardown targets |
| Worker S3 client | `services/worker-service/storage/__init__.py` | new code | Package init |
| Worker S3 client | `services/worker-service/storage/s3_client.py` | new code | boto3 S3 wrapper |
| Worker upload_artifact tool | `services/worker-service/tools/upload_artifact.py` | new code | Built-in tool implementation |
| Worker tool definitions | `services/worker-service/tools/definitions.py` | modification | Add UPLOAD_ARTIFACT_TOOL definition |
| Worker executor | `services/worker-service/executor/graph.py` | modification | Register upload_artifact tool in `_get_tools()` |
| API S3 service | `services/api-service/src/main/java/com/persistentagent/api/service/S3StorageService.java` | new code | S3 client for download/stream |
| API artifact repo | `services/api-service/src/main/java/com/persistentagent/api/repository/ArtifactRepository.java` | new code | JDBC queries for `task_artifacts` |
| API artifact controller | `services/api-service/src/main/java/com/persistentagent/api/controller/ArtifactController.java` | new code | List + download endpoints |
| API artifact model | `services/api-service/src/main/java/com/persistentagent/api/model/ArtifactMetadata.java` | new code | Artifact response DTO |
| API application config | `services/api-service/src/main/resources/application.yml` | modification | S3 endpoint config |
| API build.gradle | `services/api-service/build.gradle` | modification | Add AWS SDK for S3 |
| Console artifacts tab | `services/console/src/features/task-detail/ArtifactsTab.tsx` | new code | Artifact list + download UI |
| Console task detail | `services/console/src/features/task-detail/TaskDetailPage.tsx` | modification | Add artifacts tab |
| Console API hooks | `services/console/src/api/` | modification | Artifact API hooks |

---

## A3. Dependency Graph

```
Task 1 (DB Migration) ─┬──→ Task 3 (Worker S3 Client) ──→ Task 6 (upload_artifact Tool) ──┐
                        │                                                                    │
                        └──→ Task 4 (API Artifact Repo + S3 Service) ──→ Task 5 (API Endpoints) ──┤
                                                                                             │
Task 2 (LocalStack) ───────→ Task 3 (Worker S3 Client)                                      │
                        ┌──→ Task 4 (API S3 Service needs LocalStack endpoint)               │
                        │                                                                    │
                        └──→ Task 7 (Console Artifacts Tab) ── needs Task 5 ─────────────────┤
                                                                                             │
                             Task 8 (Integration Tests) ── needs all above ──────────────────┘
```

**Parallelization opportunities:**
- Task 1 and Task 2 can run in parallel (no code dependency)
- After Task 1+2: Tasks 3 and 4 can start in parallel
- Task 5 depends on Task 4 only
- Task 6 depends on Task 3 only
- Task 7 can start once Task 5 is complete
- Task 8 depends on all implementation tasks

---

## A4. Data / API / Schema Changes

- **New table `task_artifacts`** — stores artifact metadata: `artifact_id` (UUID PK), `task_id` (FK), `tenant_id`, `filename`, `direction` (input/output), `content_type`, `size_bytes`, `s3_key`, `created_at`. UNIQUE constraint on `(task_id, direction, filename)`.
- **New endpoint `GET /v1/tasks/{id}/artifacts`** — returns list of artifact metadata for a task. Filterable by `direction` query parameter.
- **New endpoint `GET /v1/tasks/{id}/artifacts/{filename}`** — streams artifact file from S3. Accepts optional `direction` query parameter (default: output).
- All changes are additive. No existing tables, endpoints, or behavior modified.

---

## A4.1. Task Handoff Outputs

| Task | Output |
|------|--------|
| Task 1 | Migration SQL applied; `task_artifacts` table exists in DB |
| Task 2 | LocalStack running in Docker; S3 bucket `platform-artifacts` created on startup |
| Task 3 | Python `S3Client` class with `upload()`, `download()`, `delete()` methods; functional against LocalStack |
| Task 4 | Java `ArtifactRepository` with `insert()`, `findByTaskId()`, `findByTaskIdAndFilename()` + `S3StorageService` with `download()`, `upload()` |
| Task 5 | Working `GET /v1/tasks/{id}/artifacts` and `GET /v1/tasks/{id}/artifacts/{filename}` endpoints |
| Task 6 | `upload_artifact` tool available to agents; produces S3 file + `task_artifacts` row |
| Task 7 | Console shows artifacts tab with download buttons in task detail view |
| Task 8 | E2E tests covering artifact upload via tool, listing via API, download via API |

---

## A5. Integration Points

| Caller | Callee | Interface Change | Failure Handling |
|--------|--------|-------------------|------------------|
| API ArtifactController | ArtifactRepository | New: query `task_artifacts` table | 404 if task or artifact not found |
| API ArtifactController | S3StorageService | New: stream file from S3 for download | 404 if S3 key missing; 500 if S3 unreachable |
| Worker upload_artifact tool | S3Client | New: upload content to S3 | Tool returns error message to agent on failure |
| Worker upload_artifact tool | asyncpg pool | New: insert `task_artifacts` row | Tool returns error message to agent on failure |
| Worker GraphExecutor | upload_artifact tool | New: register in `_get_tools()` | Tool only available when `upload_artifact` in `allowed_tools` |
| Console TaskDetailPage | API GET artifacts | New: fetch artifact list | Show empty state if no artifacts |

---

## A6. Deployment and Rollout

1. **Migration first** — Run `0009_artifact_storage.sql` before deploying new API/worker code. Purely additive (new table only), backward compatible.
2. **LocalStack for dev only** — Production uses real AWS S3. Endpoint configured via `S3_ENDPOINT_URL` env var. When unset, boto3/AWS SDK uses real AWS.
3. **API and worker deploy together** — Both reference the new table. Order doesn't matter since changes are additive.
4. **Console can deploy any time after API** — Frontend depends on API endpoints being available.

---

## A7. Observability

- **Structured logging:** S3 operations logged with `task_id`, `tenant_id`, `filename`, `operation`, `duration_ms`, outcome at INFO level. Failures at ERROR with exception details.
- **Upload errors:** S3 upload failures surfaced as tool errors to the agent with descriptive messages. Worker logs full exception at ERROR level.

---

## A8. Risks and Open Questions

| Risk | Mitigation |
|------|------------|
| LocalStack S3 API compatibility | Use only basic ops: PutObject, GetObject, HeadObject. LocalStack v3 handles these reliably. |
| S3 credentials in local dev | LocalStack requires no real credentials. Use dummy `test`/`test` values. |
| Artifact orphans on task failure | Artifacts retained on failure (useful for debugging). S3 lifecycle rules handle cleanup. |
| Concurrent writes for same filename | UNIQUE constraint on `(task_id, direction, filename)` prevents duplicate metadata. |

---

## A9. Orchestrator Guidance

1. Run migration (Task 1) and LocalStack setup (Task 2) first. All other tasks depend on these.
2. Worker tasks (3, 6) and API tasks (4, 5) can be parallelized after Tasks 1+2.
3. Follow existing code patterns — worker tools follow `tools/definitions.py` + `executor/graph.py`; API repositories follow `TaskRepository.java`; API controllers follow `TaskController.java`.
4. S3 client must work with both LocalStack (`http://localhost:4566`) and real AWS (no endpoint override). Use env var `S3_ENDPOINT_URL`.
5. All artifact operations must be tenant-scoped. S3 keys: `{tenant_id}/{task_id}/{direction}/{filename}`.
6. The `upload_artifact` tool is a built-in tool (like `web_search`), not an MCP tool. Registered in `_get_tools()` when `upload_artifact` in `allowed_tools`.
7. Integration tests must use LocalStack, not mock S3.
8. No sandbox concepts in this track. No `sandbox_id`, no `dead_letter_reason` extension, no agent sandbox config, no multipart submission.

---

## A10. Key Design Decisions

1. **Platform-managed S3** — Customers never touch S3 directly. All access through platform API endpoints.
2. **LocalStack for dev** — Same boto3/AWS SDK code path as production. Only endpoint URL changes.
3. **`task_artifacts` table as source of truth** — S3 is blob storage only. All metadata queries go through DB.
4. **`upload_artifact` works without sandbox** — Any agent can produce artifacts. A research agent can save a report without needing a sandbox.
5. **50 MB per artifact, 200 MB per task** — Enforced by the worker tool, not the API (since there's no file upload API in Track 1).
6. **S3 key format** — `{tenant_id}/{task_id}/{direction}/{filename}` for natural partitioning.
7. **Direction field** — `input` or `output`. Track 1 only produces `output` artifacts. `input` direction used by Track 2.

---

## Section B — Agent Task Files

| Task | File | Description |
|------|------|-------------|
| Task 1 | [task-1-db-migration.md](agent_tasks/task-1-db-migration.md) | Database migration: `task_artifacts` table |
| Task 2 | [task-2-localstack-setup.md](agent_tasks/task-2-localstack-setup.md) | LocalStack Docker setup with S3 bucket initialization |
| Task 3 | [task-3-worker-s3-client.md](agent_tasks/task-3-worker-s3-client.md) | Worker-side boto3 S3 client |
| Task 4 | [task-4-api-artifact-repo-and-s3.md](agent_tasks/task-4-api-artifact-repo-and-s3.md) | API artifact repository + S3 storage service |
| Task 5 | [task-5-api-artifact-endpoints.md](agent_tasks/task-5-api-artifact-endpoints.md) | REST endpoints for listing and downloading artifacts |
| Task 6 | [task-6-upload-artifact-tool.md](agent_tasks/task-6-upload-artifact-tool.md) | `upload_artifact` built-in agent tool |
| Task 7 | [task-7-console-artifacts-tab.md](agent_tasks/task-7-console-artifacts-tab.md) | Console artifacts tab in task detail view |
| Task 8 | [task-8-integration-tests.md](agent_tasks/task-8-integration-tests.md) | End-to-end integration tests for output artifact flow |
