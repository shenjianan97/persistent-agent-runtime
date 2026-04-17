# Agent Capabilities: Sandbox, Artifacts, and File Input

## Context

The platform today accepts a text prompt, runs an LLM loop with tools, and returns a text result. This is sufficient for simple Q&A and research tasks, but falls short for the use cases that drive real adoption of cloud-hosted agent platforms:

- **Coding agents** — need a sandbox to write, execute, and iterate on code
- **Batch document processing** — need file input/output
- **Research and analysis** — benefit from file output (reports, data)

Before investing in higher-level features like long-term memory or persistence optimizations, the platform needs these foundational capabilities so agents can actually do meaningful work.

### Target use cases

Based on research into what companies actually deploy cloud-hosted agents for, the primary use cases this platform targets are:

1. **Coding agents** — clone a repo, write/edit code, run tests, iterate, deliver a PR or zip. Typically 5-30 minutes per task. This is the strongest fit for the durable execution model (tool calls can fail, tests need iteration, CI takes time).
2. **Batch document processing** — process many documents (contracts, invoices, records) in parallel. Each task is short (minutes) but there are many of them, and the platform needs to track progress and handle failures across the batch.
3. **Research and analysis** — investigate a topic, gather information from multiple sources, synthesize a report. Benefits from web search tools and file output.

"Long-running" in practice means minutes, not hours. The real value of durability isn't sustaining multi-hour compute — it's not losing progress when things fail mid-execution.

### Current gaps addressed by this doc

| Capability | Status |
|-----------|--------|
| Code execution sandbox | Not available |
| File/artifact input | Text-only task input |
| File/artifact output | Text-only task output |

### Related work (scoped separately)

- **GitHub integration** (code agent PR workflow) — Phase 2 Track 6
- **Batch task submission** — Phase 3+ (see [design-notes.md, Section 10](../phase-3-plus/design-notes.md))
- **Webhooks** — Phase 3+ (see [design-notes.md, Section 10](../phase-3-plus/design-notes.md))
- **Structured output schemas** — Phase 3+ (see [design-notes.md, Section 10](../phase-3-plus/design-notes.md))

## 1. Code Execution Sandbox (E2B Integration)

### Approach

Integrate with [E2B](https://e2b.dev) as an external sandbox provider rather than building container orchestration in-house. E2B provides stateful, sandboxed cloud environments accessible via API. The platform manages sandbox lifecycle; the agent interacts with it through built-in tools.

This keeps the platform focused on its core value — durable execution, checkpointing, HITL, cost tracking — while outsourcing compute isolation to a purpose-built service.

### Sandbox lifecycle

```
Task claimed by worker
  → Worker provisions E2B sandbox (or skips if task doesn't need one)
  → Sandbox ID stored in task state
  → Worker downloads input artifacts into sandbox filesystem (sbx.files.write())
  → Agent uses sandbox tools (exec, read_file, write_file)
  → All tool calls route to the same sandbox via ID
  → Task enters HITL wait → Worker pauses sandbox (stops billing)
  → Task resumes → Worker resumes sandbox (auto-resume via E2B)
  → Task completes → Worker uploads output artifacts from sandbox → destroys sandbox
```

### Built-in sandbox tools

Added to the worker's built-in tool set (alongside `web_search`, `read_url`, etc.), available only when the task has a sandbox provisioned:

| Tool | Description |
|------|-------------|
| `sandbox_exec` | Execute a shell command in the sandbox, return stdout/stderr |
| `sandbox_write_file` | Write content to a file path in the sandbox |
| `sandbox_read_file` | Read content from a file path in the sandbox |
| `sandbox_download` | Download a file from the sandbox and save as output artifact (worker handles S3 upload + `task_artifacts` row) |
| `upload_artifact` | Save text/data as an output artifact directly to S3 (works with or without sandbox). Agent provides filename, content, and content_type. |

Note: input file injection into the sandbox is handled automatically by the worker at task start (see "Input file injection" below) — there is no agent-facing upload-to-sandbox tool.

### Implementation: direct SDK calls, not MCP

Sandbox tools are implemented as **direct E2B SDK calls** from the worker, not as a separate MCP server. The worker calls `sbx.commands.run()`, `sbx.files.read()`, `sbx.files.write()` etc. These are exposed to the LLM as built-in tools (same as `web_search`), but the underlying implementation is SDK calls to the E2B API.

This matches the industry pattern — every production agent platform (OpenAI, Devin, OpenHands) implements sandbox tools as direct SDK calls from the orchestrator. MCP is unnecessary overhead here since the worker already manages the sandbox lifecycle.

### Agent configuration

```json
{
  "agent_id": "code-agent-01",
  "agent_config": {
    "sandbox": {
      "enabled": true,
      "template": "python-3.11",
      "vcpu": 2,
      "memory_mb": 2048,
      "timeout_seconds": 3600
    }
  }
}
```

- `enabled`: whether to provision a sandbox for tasks under this agent
- `template`: E2B sandbox template (language/runtime environment, pre-installed dependencies)
- `vcpu`: CPU allocation (1-8 vCPUs, default 2). Customers configure based on workload — a Python script agent needs 1 vCPU, a full-stack Java project needs 4+.
- `memory_mb`: memory allocation (512-8192 MB, default 2048)
- `timeout_seconds`: max sandbox lifetime (60-86400, default 3600). Should be set >= the agent's `task_timeout_seconds` to avoid the sandbox dying before the task finishes. If the sandbox expires before the task completes, the task is dead-lettered with reason `sandbox_lost`.

### Crash recovery

The sandbox is external state not captured in LangGraph checkpoints. On worker crash:

1. Worker resumes task from last checkpoint
2. Attempts to reconnect to existing sandbox by stored ID via `Sandbox.connect(sandbox_id)`
3. If sandbox is still alive → continue (E2B sandboxes persist independently of the client)
4. If sandbox has expired → task moves to `dead_letter` with reason `sandbox_lost`

This is acceptable for now. Periodic workspace snapshots (e.g., tar + upload to artifact storage) can be added later as an optimization.

### HITL sandbox pausing

When a task enters a HITL waiting state (`waiting_for_approval` or `waiting_for_input`), the worker pauses the E2B sandbox using E2B's `onTimeout: "pause"` lifecycle configuration. This preserves the sandbox filesystem without billing for idle compute. When the task resumes (potentially on a different worker), the sandbox auto-resumes via E2B's auto-resume feature. The worker reconnects to the sandbox by stored ID and continues execution.

### Input file injection

At task start, the worker downloads input artifacts from S3 and writes them into the sandbox filesystem using the E2B SDK (`sbx.files.write(path, data)`). The agent then accesses them as regular files — it doesn't need to know they came from artifact storage. This matches the pattern used by Devin and OpenHands.

### Sandbox network access

E2B sandboxes have outbound internet access by default. This is required for agents that run `pip install`, `npm install`, `git clone`, etc. No additional configuration needed.

### E2B unavailability

If the E2B API is unreachable when the worker tries to provision a sandbox:

1. Worker retries sandbox creation with exponential backoff (3 attempts)
2. If all retries fail, the task is moved to `dead_letter` with reason `sandbox_provision_failed`
3. The task can be redriven later when E2B is available

### Code output

When the agent finishes coding, output depends on what's available:

- **Before GitHub integration (Track 6):** Agent (or worker) zips the workspace and uploads to artifact storage. Customer downloads via `GET /v1/tasks/{id}/artifacts/output.zip`.
- **After GitHub integration (Track 6):** Agent pushes a branch and opens a PR. See [Phase 2 design.md, Track 6](../phase-2/design.md).

## 2. Artifact Storage

### Approach

Platform-managed S3 bucket as the default storage backend. Artifacts served to customers through the platform API — customers never touch S3 directly.

This avoids the need for customers to configure external storage, provide AWS credentials, or set up cross-account roles. A "bring your own bucket" option can be added later as an enterprise feature.

### Storage layout

```
s3://platform-artifacts/{tenant_id}/{task_id}/input/{filename}
s3://platform-artifacts/{tenant_id}/{task_id}/output/{filename}
```

### Local development

For local dev, use [LocalStack](https://localstack.cloud/) — a local AWS emulator running in Docker. The code uses the real `boto3` S3 client pointed at `http://localhost:4566`. When deploying to real AWS, only the endpoint changes. No code path difference.

### API

#### File input

`POST /v1/tasks` gains multipart support for file attachments:

```
POST /v1/tasks
Content-Type: multipart/form-data

task_request: { "agent_id": "...", "input": "Process this document", ... }
files: [invoice.pdf, receipt.png]
```

Uploaded files are stored in the artifact bucket under the `input/` prefix. **File input requires the agent to have sandbox enabled** — the worker downloads input files into the sandbox filesystem at task start, and the agent accesses them as regular files. The API validates this at submission time: if files are attached but the agent has `sandbox.enabled: false`, the request is rejected with 400.

This keeps the design simple. Non-sandbox agents (research, Q&A) receive text-only input. File-based workloads (coding, document processing) use sandboxes.

#### Artifact listing and download

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/tasks/{id}/artifacts` | List all artifacts (input and output) |
| GET | `/v1/tasks/{id}/artifacts/{filename}` | Download a specific artifact |

Each artifact includes metadata: `filename`, `size_bytes`, `content_type`, `direction` (`input` or `output`).

#### Output artifact upload

Output artifacts can be produced in two ways:

1. **From sandbox** (sandbox agents): agent calls `sandbox_download` tool, specifying a file path in the sandbox. The worker downloads the file via `sbx.files.read()`, uploads it to S3 under the `output/` prefix, and inserts a `task_artifacts` row with the filename, `content_type` (inferred from file extension), `size_bytes`, and `s3_key`.
2. **From agent directly** (any agent): agent calls `upload_artifact` tool with content, filename, and content_type. The worker writes it to S3 and inserts a `task_artifacts` row. This works with or without a sandbox — a research agent can produce a report artifact without needing a sandbox.

The agent explicitly decides which files become output artifacts by calling these tools. The worker does not automatically zip or upload the entire sandbox filesystem.

All uploads are performed by the worker using internal S3 credentials, not by the agent or customer. The `task_artifacts` table is the source of truth for what artifacts exist — the artifact listing and download endpoints query this table.

### Task output model change

`GET /v1/tasks/{id}` includes output artifacts in the response. Input artifacts are only listed via the separate `GET /v1/tasks/{id}/artifacts` endpoint (which returns both directions).

```json
{
  "task_id": "uuid",
  "status": "completed",
  "output": {
    "result": "Analysis complete. See attached report.",
    "artifacts": [
      { "filename": "report.pdf", "size_bytes": 45230, "content_type": "application/pdf" },
      { "filename": "data.csv", "size_bytes": 12400, "content_type": "text/csv" }
    ]
  }
}
```

### Retention

Artifacts are auto-deleted after a configurable TTL using S3 lifecycle rules:

- Default: 30 days
- Configurable per-tenant
- S3 lifecycle rules handle deletion natively — no cron job or cleanup code needed
- For LocalStack in dev, retention is not enforced

### Future: customer-managed storage

When customers need data residency or compliance:

1. Customer creates S3 bucket in their AWS account
2. Customer creates cross-account IAM role granting the platform `s3:PutObject` / `s3:GetObject`
3. Customer registers role ARN + bucket via API
4. Platform calls `AssumeRole` for temporary credentials at upload time

No long-lived customer secrets stored. But this is a later addition — platform-managed storage is sufficient to launch.

## 3. Database Schema Changes

### Tasks table additions

| Column | Type | Description |
|--------|------|-------------|
| `sandbox_id` | TEXT, nullable | E2B sandbox ID for reconnection on crash recovery. Set when sandbox is provisioned, cleared on task completion. |

`sandbox_id` must be persisted in the database (not just in LangGraph checkpoint state) so that the reaper or a different worker can reconnect to or clean up the sandbox. If the worker crashes between destroying the sandbox and clearing `sandbox_id`, the reconnect attempt will fail and fall through to the "sandbox expired" dead-letter path — this is expected behavior.

The `dead_letter_reason` CHECK constraint on the `tasks` table must be extended with two new values: `sandbox_lost` (sandbox expired during crash recovery) and `sandbox_provision_failed` (E2B API unreachable after retries).

### New table: `task_artifacts`

| Column | Type | Constraints |
|--------|------|-------------|
| `artifact_id` | UUID | PK, DEFAULT gen_random_uuid() |
| `task_id` | UUID | NOT NULL, FK → tasks |
| `tenant_id` | TEXT | NOT NULL |
| `filename` | TEXT | NOT NULL |
| `direction` | TEXT | NOT NULL (`input` or `output`) |
| `content_type` | TEXT | NOT NULL |
| `size_bytes` | BIGINT | NOT NULL |
| `s3_key` | TEXT | NOT NULL |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT NOW() |

- Index on `task_id` for listing artifacts per task
- UNIQUE constraint on `(task_id, direction, filename)`

Artifact metadata lives in the database. The actual file content lives in S3. The `GET /v1/tasks/{id}/artifacts` endpoint queries this table; the download endpoint reads from S3 using the `s3_key`.

### Agent config schema addition

The `agents` table's `config` JSONB column gains the `sandbox` object:

```json
{
  "sandbox": {
    "enabled": true,
    "template": "python-3.11",
    "vcpu": 2,
    "memory_mb": 2048,
    "timeout_seconds": 3600
  }
}
```

Validated at agent creation/update time. `sandbox.enabled` defaults to `false`.

## 4. API Service Changes

The API service (Java/Spring Boot) needs these changes:

### Task submission

- `POST /v1/tasks` must accept `multipart/form-data` in addition to `application/json`
- When multipart: parse the `task_request` JSON part and `files` binary parts
- Validate: if files are attached, the target agent must have `sandbox.enabled: true` (else 400)
- Enforce size limits: 50 MB per file, 200 MB total (configure Spring Boot `spring.servlet.multipart.max-file-size` and `max-request-size` accordingly)
- Upload files to S3 under `input/` prefix, create `task_artifacts` rows

### Artifact endpoints

- `GET /v1/tasks/{id}/artifacts` — query `task_artifacts` table, return metadata list
- `GET /v1/tasks/{id}/artifacts/{filename}` — look up `s3_key` from `task_artifacts`, stream file from S3

### Agent config validation

- Agent CRUD endpoints validate the `sandbox` config block (vcpu range, memory range, timeout range)

## 5. Console UI Changes

### Task submission form

- Add a file attachment area (drag-and-drop or file picker)
- File attachment only enabled when the selected agent has `sandbox.enabled: true`
- Files sent as multipart alongside the task JSON

### Task detail view

- New "Artifacts" tab listing input and output artifacts
- Each artifact shows filename, size, content type, direction
- Download button per artifact (calls `GET /v1/tasks/{id}/artifacts/{filename}`)

### Agent configuration form

- New "Sandbox" section in agent create/edit
- Toggle for `enabled`, fields for `template`, `vcpu`, `memory_mb`, `timeout_seconds`
- Sensible defaults pre-filled (2 vCPU, 2048 MB, 3600s)

## Implementation Tracks

Implementation is split into two sequential tracks. Track 2 depends on Track 1.

### Track 1 — Output Artifact Storage

Delivers end-to-end output artifact support: agents can produce files via `upload_artifact`, users can list and download them. Establishes the S3/LocalStack infrastructure and `task_artifacts` schema that Track 2 builds on.

| # | Task | Service | Description |
|---|------|---------|-------------|
| 1 | DB migration | Database | `task_artifacts` table |
| 2 | LocalStack Docker setup | Infrastructure | S3 emulation + bucket init |
| 3 | Worker S3 client | Worker | boto3 upload/download/delete wrapper |
| 4 | API artifact repository + S3 service | API | JDBC queries for `task_artifacts`, S3 client |
| 5 | API artifact endpoints | API | `GET /v1/tasks/{id}/artifacts`, `GET /v1/tasks/{id}/artifacts/{filename}` |
| 6 | `upload_artifact` built-in tool | Worker | Agent produces output artifacts (works without sandbox) |
| 7 | Console: artifacts tab | Console | Artifact list + download in task detail view |
| 8 | Integration tests | Cross-service | End-to-end output artifact flow |

**Exec plan:** `docs/exec-plans/completed/agent-capabilities/track-1/`

### Track 2 — E2B Sandbox & File Input

Delivers sandbox code execution and file input. Agents can receive files, execute code, and produce artifacts from sandbox. Depends on Track 1 for S3 infrastructure and artifact storage.

| # | Task | Service | Description |
|---|------|---------|-------------|
| 1 | DB migration + agent sandbox config | Database + API | `sandbox_id` column, `dead_letter_reason` extension, sandbox config validation |
| 2 | E2B SDK + sandbox provisioner + lifecycle | Worker | Provision, pause, resume, destroy sandbox |
| 3 | `sandbox_exec` tool | Worker | Shell command execution in sandbox |
| 4 | `sandbox_read_file` + `sandbox_write_file` tools | Worker | File I/O in sandbox |
| 5 | `sandbox_download` tool | Worker | Sandbox file → S3 output artifact |
| 6 | Multipart task submission + input file injection | API + Worker | File upload on submit, inject into sandbox |
| 7 | Crash recovery + sandbox cost tracking | Worker | Reconnect by sandbox_id, cost integration |
| 8 | Console: file attachment + sandbox config | Console | File upload on submit, sandbox config in agent form |
| 9 | Integration tests | Cross-service | End-to-end sandbox + file input flow |

**Exec plan:** `docs/exec-plans/completed/agent-capabilities/track-2/`

### Track sequencing

Track 1 is self-contained — when complete, agents can produce output artifacts and users can download them. No sandbox concepts leak into Track 1.

Track 2 adds sandbox and file input. It uses Track 1's S3 client for `sandbox_download` and artifact storage for input file injection. The `sandbox_id` column, `dead_letter_reason` extension, sandbox agent config, multipart submission, and file attachment UI all live in Track 2.

## Dependencies

- **E2B account and API key** for sandbox integration
- **LocalStack** Docker container for local S3 (added to docker-compose)
- Phase 2 Track 4 (Custom Tool Runtime) for the tool registration patterns sandbox tools will follow

## Resolved Design Decisions

1. **Sandbox cost model** — E2B costs (~$0.05/hour for 1 vCPU, per-second billing) are rolled into the customer's existing budget tracking alongside LLM costs. Sandbox cost is added to the task's `cost_microdollars`. No separate billing system needed — sandbox costs are small relative to LLM spend.

2. **Artifact size limits** — 50 MB per file, 200 MB per task (default). Configurable per-tenant for customers with larger needs. Covers typical document processing (PDFs, CSVs) and code repos.

3. **Sandbox pre-warming** — not needed. E2B cold starts are ~500ms, which is negligible relative to task execution time. Custom templates with pre-installed dependencies handle the "slow dependency install" case. Pre-warming adds complexity for minimal benefit.

4. **Input file injection** — worker downloads artifacts from S3 and writes them into the sandbox filesystem at task start via `sbx.files.write()`. Agent sees regular files.

5. **Sandbox tools implementation** — direct E2B SDK calls from the worker, not a separate MCP server. Simpler, matches industry pattern.

6. **Sandbox timeout alignment** — sandbox `timeout_seconds` should be >= task `task_timeout_seconds`. Worker destroys sandbox immediately on task completion. Sandbox paused (not billed) during HITL waits.

## Open Questions

1. **E2B API key management** — where does the platform's E2B API key live? Worker env var for now, Secrets Manager later? (Same pattern as existing LLM provider keys.)
2. **Large file handling** — for files approaching the 50 MB limit, should the API support presigned URL upload to avoid routing large payloads through the API service?
