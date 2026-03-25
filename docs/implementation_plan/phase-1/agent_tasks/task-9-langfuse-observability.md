<!-- AGENT_TASK_START: task-9-langfuse-observability.md -->

# Task 9: Langfuse Integration and Observability Split

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope covers the Worker Service, API Service, Console, and AWS infrastructure changes required to integrate Langfuse and separate customer-facing observability from operator-facing observability.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files to understand the system architecture, the observability proposal, and the current cost tracking implementation:
1. `docs/PROJECT.md`
2. `docs/design/PHASE1_DURABLE_EXECUTION.md` (Sections 5.5, 5.7, 6.4, and 10)
3. `services/worker-service/executor/graph.py` (current manual cost tracking code to be removed)
4. `services/console/src/features/task-detail/` (current cost visualization to be updated)
5. `infrastructure/cdk/lib/compute-stack.ts` (ECS service definitions)

**CRITICAL POST-WORK:** After completing this task, you MUST update the status of this task to "Done" in the `docs/implementation_plan/phase-1/progress.md` file.

## Context
Phase 1 was originally designed with OpenTelemetry for observability, but this was never implemented. The current state is:
- **Implemented:** Manual `CostTrackingCallback` in `graph.py` (~150 lines) that extracts token usage from LLM stream events, calculates cost from a pricing lookup against the `models` table, and writes `cost_microdollars` to the `checkpoints` table.
- **Implemented:** Structured worker logging with counters/gauges in `core/logging.py` (not exported to any metrics backend).
- **Implemented:** Console displays cost data from checkpoint rows and the task's `total_cost_microdollars` field.
- **Not implemented:** No OpenTelemetry SDK, no CloudWatch metric export, no trace collection.

This task replaces the manual cost tracking with Langfuse auto-instrumentation and establishes a clear two-layer observability model:
- **Customer-facing (Langfuse):** Per-LLM-call traces, token usage, cost, latency, tool call I/O — served to the Console via the Langfuse REST API through the API Service.
- **Operator-facing (CloudWatch):** Structured logs, platform metrics (queue depth, lease expiry, worker saturation), alerts — not exposed in the Console.

## Affected Components
- **Service/Module:** Worker Service (Python), API Service (Java), Console (React), AWS Infrastructure (CDK)
- **File paths:**
  - `services/worker-service/executor/graph.py` — remove manual cost tracking, add Langfuse callback
  - `services/worker-service/requirements.txt` or `pyproject.toml` — add `langfuse` dependency
  - `services/api-service/` — add Langfuse REST API proxy endpoints
  - `services/console/src/features/task-detail/` — update cost/trace visualization to use Langfuse data
  - `infrastructure/cdk/` — add Langfuse workloads and required backing services
  - `infrastructure/database/migrations/` — migration to drop `cost_microdollars` and `execution_metadata` from `checkpoints`
- **Change type:** modification (Worker, API, Console), new code (Langfuse infrastructure)

## Dependencies
- **Must complete first:** Tasks 1–8 (all existing Phase 1 tasks are done)
- **Provides output to:** None (this is a follow-up refinement)
- **Shared interfaces/contracts:** Replaces the `cost_microdollars`/`execution_metadata` contract between Worker, API, and Console with a Langfuse trace query contract

## Implementation Specification

### Step 1: Deploy Self-Hosted Langfuse (CDK Infrastructure)

**1a. Langfuse backing stores (Data stack — `infrastructure/cdk/lib/data-stack.ts`):**
- Provision the backing services required by the pinned Langfuse release: PostgreSQL, Redis/Valkey, and ClickHouse (or managed equivalents supported by Langfuse). Do not assume PostgreSQL alone is sufficient.
- If reusing the existing Aurora Serverless v2 cluster for Langfuse's PostgreSQL catalog, create a dedicated database named `langfuse` via a non-transactional existence-check/create flow (for example: query `pg_database`, then issue `CREATE DATABASE langfuse` only when absent).
- Do not use `CREATE DATABASE IF NOT EXISTS`, and do not run `CREATE DATABASE` inside the schema bootstrap transaction wrapper. If Aurora reuse becomes awkward, provision a dedicated PostgreSQL instance for Langfuse instead.

**1b. Langfuse secrets (Data stack or Compute stack):**
- Create a new Secrets Manager secret (e.g., `langfuse-credentials`) containing: `NEXTAUTH_SECRET`, `SALT`, `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`, `LANGFUSE_INIT_PROJECT_SECRET_KEY`.
- These are generated once at stack creation time (use `secretsmanager.Secret` with `generateSecretString`).

**1c. Langfuse ECS workloads (Compute stack — `infrastructure/cdk/lib/compute-stack.ts`):**
- Add the Langfuse web workload and the required background worker workload using a pinned Langfuse release (not `latest`).
- Task definition environment variables must include the exact PostgreSQL, Redis/Valkey, and ClickHouse connection settings required by the pinned Langfuse release, plus:
  - `NEXTAUTH_URL` — internal ALB URL with `/langfuse` path
  - `NEXTAUTH_SECRET`, `SALT` — from the new Langfuse Secrets Manager secret
  - `LANGFUSE_INIT_PROJECT_NAME` — hardcoded (e.g., `persistent-agent-runtime`)
  - `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`, `LANGFUSE_INIT_PROJECT_SECRET_KEY` — from the Langfuse secret
- If the Langfuse migration user cannot create databases, also configure the shadow/direct migration database URLs Langfuse expects for Prisma migrations.
- Container port 3000, health check on `/api/public/health`.
- Security group: allow inbound from the ALB and from Worker/API service security groups.

**1d. ALB routing (Compute stack):**
- Add a new ALB target group for the Langfuse service.
- Add a new listener rule on the internal ALB: path pattern `/langfuse/*` → Langfuse target group (priority before the Console catch-all rule).

**1e. Worker and API Service environment variables (Compute stack):**
- Add to the Worker Service ECS task definition:
  - `LANGFUSE_PUBLIC_KEY` — from Langfuse Secrets Manager secret
  - `LANGFUSE_SECRET_KEY` — from Langfuse Secrets Manager secret
  - `LANGFUSE_HOST` — internal ALB URL (e.g., `http://<alb-dns>/langfuse`)
- Add to the API Service ECS task definition:
  - `LANGFUSE_PUBLIC_KEY` — from Langfuse Secrets Manager secret
  - `LANGFUSE_SECRET_KEY` — from Langfuse Secrets Manager secret
  - `LANGFUSE_HOST` — internal ALB URL

**1f. IAM permissions:**
- Grant the Worker and API Service task roles `secretsmanager:GetSecretValue` on the Langfuse secret ARN.

**1g. Ensure Langfuse is not exposed to the public internet — it runs behind the same internal ALB as the API and Console.**

### Step 2: Worker Service — Replace Manual Cost Tracking with Langfuse Callback
2a. Add `langfuse` to the Worker Service dependencies.

2b. In `graph.py`, replace the manual cost tracking with Langfuse's LangChain callback handler:
```python
from langfuse.callback import CallbackHandler

langfuse_handler = CallbackHandler(
    public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
    secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    host=os.environ["LANGFUSE_HOST"],  # internal ALB URL
    trace_name=f"task-{task_id}",
    session_id=task_id,
    metadata={"agent_id": agent_id, "tenant_id": tenant_id, "worker_id": worker_id},
)

result = await graph.ainvoke(input, config={
    "configurable": {"thread_id": task_id},
    "callbacks": [langfuse_handler],
})
```

2c. Remove the following functions from `graph.py` (approximately 150 lines):
- `_extract_cost_from_stream_event()`
- `_extract_cost_from_checkpoint_payload()`
- `_extract_usage_from_stream_event()` / `_extract_usage_from_update()` / `_extract_usage_from_message()`
- `_coerce_usage_value()`
- `_calculate_cost_microdollars()` / `_cost_from_tokens()`
- `_persist_checkpoint_cost()`
- `_backfill_checkpoint_costs()`

2d. Remove the post-super-step cost UPDATE query that writes `cost_microdollars` and `execution_metadata` to the checkpoints table.

2e. Verify that checkpoint persistence (via `PostgresDurableCheckpointer`) and all recovery/resume behavior remain unchanged — cost tracking removal must not affect execution correctness.

### Step 3: Configure Langfuse Model Pricing
3a. Seed Langfuse's model pricing registry with the same per-token pricing data currently in the `models` table. This can be done via the Langfuse API at startup or as a one-time configuration.

3b. This ensures Langfuse calculates cost automatically from token usage — the Worker does not need to calculate cost itself.

### Step 4: API Service — Add Langfuse Trace Proxy Endpoints
4a. Add endpoints that proxy trace data from Langfuse to the Console. The Console should never call Langfuse directly — the API Service mediates all access:
- `GET /v1/tasks/{task_id}/traces` — returns the Langfuse trace tree for a task (using Langfuse `sessionId = task_id` as the lookup key)
- `GET /v1/tasks/{task_id}/cost` — returns aggregated cost and token usage for a task from Langfuse

4b. The API Service calls the Langfuse REST API (`GET /api/public/traces?sessionId={task_id}`, `GET /api/public/sessions/{sessionId}`) using HTTP Basic Auth with both credentials (username = `LANGFUSE_PUBLIC_KEY`, password = `LANGFUSE_SECRET_KEY`).

4c. Update the existing `GET /v1/tasks/{task_id}` response: the `total_cost_microdollars` field should now be populated by querying Langfuse (or returned as `null` with a separate `/cost` endpoint). Choose the approach that minimizes latency on the main status endpoint.

4d. Update the `GET /v1/tasks/{task_id}/checkpoints` response: remove `cost_microdollars` and `execution_metadata` from individual checkpoint objects. Trace data per checkpoint is available via the `/traces` endpoint.

### Step 5: Database Migration — Remove Cost Columns from Checkpoints

**5a. Local migration file:**
Create `0005_remove_checkpoint_cost.sql` in `infrastructure/database/migrations/` (note: `0004_timeout_reference.sql` already exists):
```sql
ALTER TABLE checkpoints DROP COLUMN IF EXISTS cost_microdollars;
ALTER TABLE checkpoints DROP COLUMN IF EXISTS execution_metadata;
```

**5b. AWS deployment path:**
The CDK schema bootstrap Lambda (`infrastructure/cdk/lib/schema-bootstrap/handler.ts`) automatically reads all `NNNN_*.sql` files from `infrastructure/database/migrations/`, bundles them at build time, and applies them in order via the `schema_migrations` tracking table. No CDK code changes are needed for this migration — adding the file to `infrastructure/database/migrations/` is sufficient. On next `cdk deploy`, the Data stack's custom resource will detect the new migration (via `MigrationsChecksum` change) and run it against Aurora.

**5c. Application code updates:**
- Update the `PostgresDurableCheckpointer` `put()` and `get_tuple()` methods to no longer reference `cost_microdollars` or `execution_metadata` columns.
- Update the API Service's checkpoint query and `CheckpointResponse` DTO to no longer select or expose these columns.
- Verify the local `make db-reset-verify` flow still works with the new migration.

### Step 6: Console — Update Cost and Trace Visualization
6a. Update the task detail page to fetch cost data from the new `/cost` or `/traces` endpoint instead of reading `cost_microdollars` from checkpoint rows.

6b. Add a trace view to the task detail page showing:
- Per-LLM-call details: model, token usage (input/output), cost, latency
- Tool call sequences with inputs and outputs
- Full trace tree for the task execution

6c. Keep the existing cost summary panel and cost-per-step bar chart, but source the data from Langfuse traces instead of checkpoint rows.

6d. Remove the system health overview widgets (DB health, active workers, queued tasks) from the Console dashboard. These are operator concerns and belong in CloudWatch. Replace with a customer-appropriate landing page (e.g., recent tasks, quick submit).

### Step 7: Console — Separate Operator vs Customer Concerns
7a. Remove or move behind a feature flag any platform-internal displays:
- System status indicator from the header (DB status, worker count)
- Queue depth displays
- The `/v1/health` polling and health indicator

7b. The Console should focus on:
- Task submission and status
- Checkpoint timeline
- Execution traces and cost (from Langfuse)
- Dead letter queue with retry/redrive
- Error details and recovery history

### Step 8: Operator Observability — CloudWatch Metric Export and CDK Resources

**8a. Worker Service metric export (Python code):**
Add `aws-embedded-metrics` to the Worker Service dependencies. Wire the existing counters/gauges in `core/logging.py` to emit CloudWatch Embedded Metric Format (EMF) logs. ECS Fargate with `awslogs` driver automatically routes these to CloudWatch, where EMF-formatted log lines are extracted as CloudWatch metrics — no collector, agent, or PutMetricData calls needed.

Metrics to export:
```
tasks.submitted         -- counter, by agent_id
tasks.completed         -- counter, by agent_id
tasks.dead_letter       -- counter, by agent_id, by error_type
tasks.active            -- gauge, by agent_id
nodes.duration_ms       -- histogram, by node_name
workers.active_tasks    -- gauge, by worker_id
queue.depth             -- gauge
poll.empty              -- counter, by worker_id
leases.expired          -- counter
heartbeats.missed       -- counter, by worker_id
```

**8b. CloudWatch dashboard (CDK — `infrastructure/cdk/lib/compute-stack.ts` or a new `observability-stack.ts`):**
Add a `cloudwatch.Dashboard` CDK construct with widgets for:
- Queue depth (gauge)
- Active tasks by agent (gauge)
- Dead letter count (counter)
- Lease expiry rate (counter)
- Worker active tasks (gauge per worker)
- Poll empty frequency (counter)

This is the operator-facing dashboard — not exposed to customers.

**8c. CloudWatch alarms (CDK):**
Add `cloudwatch.Alarm` constructs for:
- Dead letter accumulation: `tasks.dead_letter` count > 0 for > 5 min → P2 severity
- Lease expiry spikes: `leases.expired` rate > 10/min → P2 severity
- Optionally wire to an SNS topic for email/PagerDuty notifications (topic creation can be deferred).

**8d. IAM permissions:**
- The Worker Service task role already has `logs:CreateLogStream` and `logs:PutLogEvents` for CloudWatch Logs (from Task 8 CDK setup). EMF metrics are extracted from logs automatically — no additional IAM grants needed.

### Step 9: End-to-End Verification

**9a. Local verification:**
- Run `make db-reset-verify` to confirm the `0005` migration applies cleanly
- Run Worker, API, and Console locally with a local Langfuse instance (via `docker compose` or direct container run)
- Execute a full task and verify Langfuse captures traces, Console displays them, and checkpoints no longer have cost columns
- Run existing tests to verify checkpoint-resume and crash recovery are unaffected

**9b. AWS deployment verification (`cdk deploy`):**
- Verify the Data stack deploys the `0005` migration via the schema bootstrap Lambda (check Lambda logs for `0005_remove_checkpoint_cost.sql applied`)
- Verify the Compute stack creates the Langfuse ECS service, target group, and ALB listener rule
- Verify the Langfuse health check passes: `curl http://<alb-dns>/langfuse/api/public/health`
- Verify Worker and API services start with `LANGFUSE_*` environment variables populated from Secrets Manager
- Submit a task via the Console → verify Langfuse traces appear in the Console task detail view
- Verify the CloudWatch dashboard is created and receives platform metrics from the Worker Service
- Verify CloudWatch alarms are in `OK` state (no dead letter accumulation)

**9c. Regression check:**
- Verify existing CDK tests still pass: `cd infrastructure/cdk && npm test -- --runInBand`
- Verify the API Service test suite passes with the updated checkpoint DTO (no cost columns)
- Verify the Worker Service test suite passes without the manual cost tracking code

## Acceptance Criteria
The implementation is complete when:
- [ ] Langfuse runs as self-hosted ECS workloads behind the internal ALB (CDK Compute stack)
- [ ] Langfuse's required backing services (PostgreSQL, Redis/Valkey, ClickHouse, or managed equivalents supported by Langfuse) are provisioned and wired correctly
- [ ] Langfuse secrets are stored in AWS Secrets Manager and injected into Worker/API task definitions
- [ ] Worker Service uses `langfuse.callback.CallbackHandler` instead of manual `CostTrackingCallback`
- [ ] All manual cost extraction/calculation/backfill code is removed from `graph.py`
- [ ] `0005_remove_checkpoint_cost.sql` migration exists in `infrastructure/database/migrations/` and is applied by the CDK schema bootstrap Lambda on deploy
- [ ] `cost_microdollars` and `execution_metadata` columns are removed from the `checkpoints` table
- [ ] API Service proxies Langfuse trace data to the Console via new endpoints
- [ ] Console displays per-task cost, token usage, and trace tree from Langfuse data
- [ ] Console no longer shows platform health internals (DB status, worker count, queue depth)
- [ ] Worker platform metrics are exported to CloudWatch via EMF
- [ ] CloudWatch dashboard and alarms are deployed via CDK
- [ ] `cdk deploy` succeeds with all new resources (Langfuse workloads, backing services, ALB rule, dashboard, alarms)
- [ ] Existing CDK tests pass with the new Compute stack resources
- [ ] Checkpoint-resume and crash recovery behavior is unaffected (existing tests pass)
- [ ] End-to-end task execution on AWS produces correct Langfuse traces and Console visualization

## Testing Requirements
- **CDK tests:** Verify Compute stack synthesizes with the new Langfuse workloads, ALB rule, dashboard, and alarms. Verify Data stack still synthesizes cleanly with the required Langfuse backing-store resources and migration bundling picks up `0005`.
- **Worker Service unit tests:** Verify Langfuse callback is registered during graph invocation. Verify manual cost functions are removed (no import, no call).
- **Worker Service integration tests:** Run a task against a real LLM (or mock) and verify Langfuse receives trace data. Verify checkpoint rows no longer contain cost columns.
- **API Service tests:** Verify `/v1/tasks/{task_id}/traces` and `/v1/tasks/{task_id}/cost` return data from Langfuse. Verify checkpoint response no longer includes cost fields.
- **Console tests:** Verify cost panel sources data from the new trace/cost endpoints. Verify platform health widgets are removed.
- **Migration tests:** Verify `0005_remove_checkpoint_cost.sql` runs cleanly via `make db-reset-verify` on an existing database with data.
- **AWS deployment:** `cdk deploy` succeeds, Langfuse health check passes, task submission produces traces visible in Console.

## Constraints and Guardrails
- Langfuse must be self-hosted — no external SaaS. All trace data stays within the AWS environment.
- The Console must never call Langfuse directly — all access goes through the API Service.
- Checkpoint persistence and crash recovery must remain unchanged. This task only affects observability, not execution correctness.
- Do not modify the `PostgresDurableCheckpointer`'s `put()` lease-check logic — only remove cost-related column references.
- Keep the `models` table and its pricing columns — the same pricing data seeds Langfuse's model registry.

## Assumptions / Open Questions for This Task
- RESOLVED: Langfuse public REST API uses HTTP Basic Auth (public key as username, secret key as password). The API Service therefore needs both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`.
- RESOLVED: `CREATE DATABASE` must be handled outside transactional migration SQL and without `IF NOT EXISTS`.
- RESOLVED: The official Langfuse self-hosting distributions include additional backing services beyond PostgreSQL (at minimum Redis/Valkey and ClickHouse), so the CDK design must account for them or compatible managed equivalents.
- ASSUMPTION: The `langfuse` Python SDK supports `asyncio` via `ainvoke` / `astream` callback handlers.
- OPEN QUESTION: Reuse the existing Aurora cluster for Langfuse's PostgreSQL catalog, or provision a dedicated PostgreSQL instance for cleaner isolation?
- ASSUMPTION: The CDK schema bootstrap Lambda's migration bundling (`infrastructure/database/migrations/`) automatically picks up `0005_remove_checkpoint_cost.sql` on the next `cdk deploy` — no handler changes needed.
- OPEN QUESTION: Should `total_cost_microdollars` on the task status response be populated lazily from Langfuse on each API call, or cached/aggregated periodically? Lazy is simpler but adds latency; caching adds complexity. Start with lazy and optimize if needed.
- OPEN QUESTION: Langfuse container version pinning — use a specific release tag rather than `latest` for reproducibility. Check the latest stable release at deploy time and pin it in CDK.

<!-- AGENT_TASK_END: task-9-langfuse-observability.md -->
