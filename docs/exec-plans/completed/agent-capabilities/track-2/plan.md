# Agent Capabilities — Track 2: E2B Sandbox & File Input

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable agents to receive input files and execute code in an isolated E2B sandbox, with built-in tools for shell execution, file I/O, and artifact production from sandbox files.

**Architecture:** The worker provisions an E2B sandbox per task (when the agent has `sandbox.enabled: true`), manages its lifecycle (create, pause on HITL, resume, destroy), and exposes sandbox operations as built-in tools. Input files are uploaded via multipart task submission, stored in S3 (Track 1 infrastructure), and injected into the sandbox filesystem at task start. Sandbox files can be exported as output artifacts via `sandbox_download`. On crash recovery, the worker reconnects to the sandbox by stored `sandbox_id` or dead-letters the task if the sandbox has expired.

**Tech Stack:** E2B SDK (Python sandbox client), PostgreSQL (sandbox_id storage, agent config), Spring Boot (multipart submission, agent config validation), Python asyncpg + boto3 (worker sandbox + artifact integration), React/TypeScript (console file upload + sandbox config)

**Depends on:** Track 1 (Output Artifact Storage) — uses S3 client, artifact service, and `task_artifacts` table.

---

## A1. Implementation Overview

Track 2 extends the runtime with sandbox code execution and file input:

1. **Database migration + agent sandbox config** — `sandbox_id` column on tasks, `dead_letter_reason` extension, sandbox config validation in agent CRUD
2. **E2B SDK setup + sandbox provisioner + lifecycle manager** — provision, pause, resume, destroy sandbox with state tracking
3. **`sandbox_exec` tool** — execute shell commands in the sandbox
4. **`sandbox_read_file` + `sandbox_write_file` tools** — file I/O in the sandbox
5. **`sandbox_download` tool** — export a sandbox file as an output artifact (sandbox → S3 via Track 1's artifact service)
6. **Multipart task submission + input file injection** — accept file attachments on `POST /v1/tasks`, inject into sandbox at task start
7. **Crash recovery + sandbox cost tracking** — reconnect by `sandbox_id`, dead-letter on sandbox loss, add E2B cost to `cost_microdollars`
8. **Console UI** — file attachment on task submit, sandbox config section in agent form
9. **Integration tests** — end-to-end sandbox + file input flow

**Canonical design contract:** `docs/design-docs/agent-capabilities/design.md`

---

## A2. Impacted Components

| Component | Path | Change Type | Description |
|-----------|------|-------------|-------------|
| DB migration | `infrastructure/database/migrations/0010_sandbox_support.sql` | new migration | `sandbox_id` column, `dead_letter_reason` extension |
| Worker sandbox provisioner | `services/worker-service/sandbox/__init__.py` | new code | Package init |
| Worker sandbox provisioner | `services/worker-service/sandbox/provisioner.py` | new code | E2B sandbox lifecycle (create, connect, pause, resume, destroy) |
| Worker sandbox tools | `services/worker-service/tools/sandbox_tools.py` | new code | `sandbox_exec`, `sandbox_read_file`, `sandbox_write_file`, `sandbox_download` |
| Worker tool definitions | `services/worker-service/tools/definitions.py` | modification | Add sandbox tool definitions |
| Worker executor | `services/worker-service/executor/graph.py` | modification | Sandbox provisioning, tool registration, input file injection, crash recovery, cost tracking |
| Worker pyproject.toml | `services/worker-service/pyproject.toml` | modification | Add `e2b-code-interpreter` dependency |
| API task controller | `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` | modification | Multipart task submission |
| API task service | `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` | modification | Handle file uploads, validate sandbox requirement |
| API agent service | `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` | modification | Validate sandbox config block |
| API agent request model | `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentCreateRequest.java` | modification | Add sandbox config fields |
| API application config | `services/api-service/src/main/resources/application.properties` | modification | Multipart size limits |
| Console file attachment | `services/console/src/features/submit/FileAttachment.tsx` | new code | Drag-and-drop file upload component |
| Console submit page | `services/console/src/features/submit/SubmitTaskPage.tsx` | modification | Integrate file attachment, multipart submission |
| Console submit schema | `services/console/src/features/submit/schema.ts` | modification | File validation rules |
| Console agent dialog | `services/console/src/features/agents/CreateAgentDialog.tsx` | modification | Sandbox config section |
| Console allowed tools | `services/console/src/features/submit/schema.ts` | modification | Add sandbox tools to ALLOWED_TOOLS |

---

## A3. Dependency Graph

```
Task 1 (DB + Sandbox Config) ─┬──→ Task 2 (Sandbox Provisioner + Lifecycle) ──→ Task 3 (sandbox_exec) ──┐
                               │                                                                          │
                               │     Task 2 ──→ Task 4 (sandbox_read_file + sandbox_write_file) ──────────┤
                               │                                                                          │
                               │     Task 3 ──→ Task 5 (sandbox_download) ── needs Track 1 S3Client ──────┤
                               │                                                                          │
                               ├──→ Task 6 (Multipart Submission + File Injection) ── needs Task 2 ───────┤
                               │                                                                          │
                               └──→ Task 7 (Crash Recovery + Cost) ── needs Task 2 ──────────────────────┤
                                                                                                          │
                                    Task 8 (Console UI) ── needs Task 1 (sandbox config) ─────────────────┤
                                                                                                          │
                                    Task 9 (Integration Tests) ── needs all above ────────────────────────┘
```

**Parallelization opportunities:**
- Task 1 is the entry point; all other tasks depend on it
- After Task 1: Tasks 2 and 8 can start in parallel
- After Task 2: Tasks 3, 4, 6, 7 can all start in parallel (but NOT Task 5 — it depends on Task 3)
- After Task 3: Task 5 can start (Task 5 depends on Task 3 because both modify `_get_tools()` in `graph.py`)
- Task 9 depends on all implementation tasks

---

## A4. Data / API / Schema Changes

- **New column `tasks.sandbox_id`** — TEXT, nullable. Stores E2B sandbox ID for reconnection on crash recovery. Set when sandbox is provisioned, cleared on task completion.
- **Extended `dead_letter_reason` CHECK** — adds `sandbox_lost` (sandbox expired during crash recovery) and `sandbox_provision_failed` (E2B API unreachable after retries).
- **Modified agent config JSONB** — adds optional `sandbox` object: `enabled` (bool, default false), `template` (string), `vcpu` (int, 1-8, default 2), `memory_mb` (int, 512-8192, default 2048), `timeout_seconds` (int, 60-86400, default 3600).
- **Modified `POST /v1/tasks`** — accepts `multipart/form-data` with file attachments in addition to `application/json`. Files rejected if target agent has `sandbox.enabled: false`.
- **File size limits** — 50 MB per file, 200 MB total per request. Configured via Spring Boot `spring.servlet.multipart.max-file-size` and `max-request-size`.
- All changes backward compatible. JSON-only task submission continues to work. Agents without sandbox config default to `sandbox.enabled: false`.

---

## A4.1. Task Handoff Outputs

| Task | Output |
|------|--------|
| Task 1 | Migration applied (`sandbox_id` column, extended `dead_letter_reason`); agent CRUD validates sandbox config |
| Task 2 | `SandboxProvisioner` class: `provision()`, `connect()`, `pause()`, `resume()`, `destroy()` with E2B SDK |
| Task 3 | `sandbox_exec` tool: runs shell commands, returns stdout/stderr |
| Task 4 | `sandbox_read_file` + `sandbox_write_file` tools: file I/O in sandbox |
| Task 5 | `sandbox_download` tool: copies sandbox file → S3 output artifact using Track 1 artifact service |
| Task 6 | Multipart `POST /v1/tasks` with files; worker injects input artifacts into sandbox at task start |
| Task 7 | Worker reconnects to sandbox by `sandbox_id` on crash; dead-letters on sandbox loss; E2B cost tracked |
| Task 8 | Console: file attachment on submit (multipart), sandbox config section in agent form |
| Task 9 | E2E tests: sandbox lifecycle, tool execution, file injection, crash recovery |

---

## A5. Integration Points

| Caller | Callee | Interface Change | Failure Handling |
|--------|--------|-------------------|------------------|
| Worker GraphExecutor | SandboxProvisioner | New: provision sandbox at task start | Retry 3x with backoff; dead-letter with `sandbox_provision_failed` |
| Worker GraphExecutor | SandboxProvisioner | New: pause sandbox on HITL wait | Log warning if pause fails; sandbox timeout will handle cleanup |
| Worker GraphExecutor | SandboxProvisioner | New: resume sandbox on task resume | If sandbox expired → dead-letter with `sandbox_lost` |
| Worker GraphExecutor | SandboxProvisioner | New: destroy sandbox on task completion | Best-effort; E2B auto-expires if destroy fails |
| Worker sandbox tools | SandboxProvisioner | New: get sandbox instance for tool execution | Tools receive sandbox reference via closure |
| Worker sandbox_download | Track 1 S3Client + asyncpg | New: upload file to S3, insert `task_artifacts` row | Tool returns error to agent on failure |
| Worker GraphExecutor | Track 1 S3 client | New: download input artifacts from S3 at task start | Dead-letter if input files unretrievable |
| API TaskController | S3StorageService | New: upload files to S3 on multipart submission | 500 if S3 unreachable; partial uploads cleaned up |
| API TaskController | AgentService | New: validate `sandbox.enabled` when files attached | 400 if files attached but sandbox not enabled |
| API AgentService | AgentService | Modified: validate sandbox config block | 400 with field-level errors for invalid config |
| Console SubmitTaskPage | API POST /v1/tasks | Modified: multipart form data with files | Show upload error; disable submit during upload |
| Console AgentDialog | API agent CRUD | Modified: include sandbox config | Validate sandbox fields client-side |

---

## A6. Deployment and Rollout

1. **Track 1 must be complete** — Track 2 depends on S3 infrastructure, `task_artifacts` table, and artifact service from Track 1.
2. **Migration first** — Run `0010_sandbox_support.sql` before deploying new code. Additive changes only.
3. **E2B API key required** — Worker needs `E2B_API_KEY` environment variable. For local dev, obtain a key from [e2b.dev](https://e2b.dev).
4. **API and worker deploy together** — Both need the new schema and config validation.
5. **Console can deploy any time after API** — Frontend depends on API endpoints.
6. **Sandbox feature is opt-in** — Only agents with `sandbox.enabled: true` get sandboxes. Existing agents unaffected.

---

## A7. Observability

- **Sandbox lifecycle:** Log provision, pause, resume, destroy events with `task_id`, `sandbox_id`, `duration_ms` at INFO level.
- **Sandbox tools:** Log each tool call with `task_id`, `sandbox_id`, `tool_name`, `duration_ms`, outcome at INFO level.
- **E2B errors:** Log E2B API failures at ERROR with full context (operation, sandbox_id, error message).
- **Cost tracking:** Log sandbox cost additions with `task_id`, `sandbox_duration_seconds`, `cost_microdollars` at INFO level.
- **Crash recovery:** Log reconnect attempts and outcomes with `task_id`, `sandbox_id`, `success`/`dead_letter_reason` at WARN level.

---

## A8. Risks and Open Questions

| Risk | Mitigation |
|------|------------|
| E2B API unavailability | 3 retries with exponential backoff before dead-lettering with `sandbox_provision_failed`. Task can be redriven later. |
| Sandbox timeout during long HITL waits | Sandbox paused during HITL (not billed). E2B pause preserves state. If sandbox expires anyway → dead-letter with `sandbox_lost`. |
| E2B SDK version compatibility | Pin exact version in `pyproject.toml`. E2B SDK is stable with semantic versioning. |
| Sandbox cost spikes | Costs tracked in `cost_microdollars` alongside LLM costs. Budget limits from Phase 2 Track 3 apply. |
| Large file injection slowing task start | Input files limited to 200 MB total. Injection is sequential `sbx.files.write()` calls. Acceptable for typical workloads. |

---

## A9. Orchestrator Guidance

1. Task 1 is the entry point. All other tasks depend on it.
2. After Task 1, Tasks 2 and 8 (Console) can start in parallel.
3. After Task 2 (sandbox provisioner), Tasks 3, 4, 6, 7 can start in parallel (but NOT Task 5 — it depends on Task 3 because both modify `_get_tools()` in `graph.py`).
4. Sandbox tools follow the same registration pattern as existing built-in tools in `_get_tools()` — check `allowed_tools` list.
5. Sandbox tools are conditional: only registered when the task's agent has `sandbox.enabled: true` AND the tool name is in `allowed_tools`.
6. The sandbox provisioner manages the E2B SDK lifecycle. Tools receive a sandbox reference, never call E2B SDK directly.
7. Use Track 1's `S3Client` and `ArtifactRepository` for `sandbox_download` — do not create separate S3 code.
8. Input file injection happens in `execute_task()` after sandbox provisioning, before the LLM loop starts.
9. Crash recovery happens in `execute_task()` when a task resumes from checkpoint — attempt `Sandbox.connect(sandbox_id)` before re-entering the LLM loop.
10. E2B API key is read from `E2B_API_KEY` environment variable. No secrets management integration in this track.

---

## A10. Key Design Decisions

1. **E2B over self-hosted containers** — Outsource compute isolation to a purpose-built service. Platform stays focused on durable execution, checkpointing, HITL.
2. **Direct SDK calls, not MCP** — Sandbox tools implemented as direct E2B SDK calls from the worker. Matches industry pattern (OpenAI, Devin, OpenHands). Simpler than a separate MCP server.
3. **Sandbox per task** — Each task gets its own sandbox. No sharing between tasks. Sandbox destroyed on task completion.
4. **HITL pause** — Sandbox paused (not billed) when task enters HITL wait. Auto-resumed when task resumes. Uses E2B `onTimeout: "pause"` lifecycle config.
5. **Crash recovery via sandbox_id** — `sandbox_id` stored in tasks table (not just checkpoint). Enables reconnect from any worker. If sandbox expired → dead-letter, not retry.
6. **File input requires sandbox** — API rejects file attachments for agents without `sandbox.enabled: true`. Keeps the file injection path simple.
7. **Sandbox timeout >= task timeout** — `sandbox.timeout_seconds` should be >= `task_timeout_seconds`. At agent config time, `sandbox.timeout_seconds` is validated to be a reasonable minimum (>= 60s). The runtime cross-validation (sandbox timeout >= task timeout) happens at task submission time in Task 6, because `task_timeout_seconds` is per-task, not per-agent.
8. **No sandbox pre-warming** — E2B cold starts ~500ms. Negligible relative to task execution time. Custom templates handle dependency pre-installation.

---

## Section B — Agent Task Files

| Task | File | Description |
|------|------|-------------|
| Task 1 | [task-1-db-and-sandbox-config.md](agent_tasks/task-1-db-and-sandbox-config.md) | DB migration (`sandbox_id`, `dead_letter_reason`) + agent sandbox config validation |
| Task 2 | [task-2-sandbox-provisioner.md](agent_tasks/task-2-sandbox-provisioner.md) | E2B SDK setup + sandbox provisioner + lifecycle manager |
| Task 3 | [task-3-sandbox-exec-tool.md](agent_tasks/task-3-sandbox-exec-tool.md) | `sandbox_exec` built-in tool |
| Task 4 | [task-4-sandbox-file-tools.md](agent_tasks/task-4-sandbox-file-tools.md) | `sandbox_read_file` + `sandbox_write_file` built-in tools |
| Task 5 | [task-5-sandbox-download-tool.md](agent_tasks/task-5-sandbox-download-tool.md) | `sandbox_download` tool: sandbox file → S3 output artifact |
| Task 6 | [task-6-multipart-and-injection.md](agent_tasks/task-6-multipart-and-injection.md) | Multipart task submission + input file injection into sandbox |
| Task 7 | [task-7-crash-recovery-and-cost.md](agent_tasks/task-7-crash-recovery-and-cost.md) | Crash recovery by sandbox_id + E2B cost tracking |
| Task 8 | [task-8-console-file-and-sandbox.md](agent_tasks/task-8-console-file-and-sandbox.md) | Console: file attachment on submit, sandbox config in agent form |
| Task 9 | [task-9-integration-tests.md](agent_tasks/task-9-integration-tests.md) | End-to-end sandbox + file input integration tests |
