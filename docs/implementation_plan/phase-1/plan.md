# Phase 1 Implementation Orchestrator Prompt

**Role**: You are the Orchestrator Agent responsible for overseeing the execution of the Phase 1 implementation of the Persistent Agent Runtime.

**CRITICAL PRE-WORK:** Before delegating any tasks or making technical decisions, you MUST read the following context files to understand the project goals and system architecture:
1. `docs/PROJECT.md`
2. `docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md`

Your responsibilities are to assign the individual tasks listed in Section B to specialized coding agents, track their progress in `progress.md`, and resolve any dependencies or blockers based on the architecture described in the design documents.

---

### SECTION A — Implementation Plan

#### A1. Implementation Overview
Phase 1 Durable Execution will be established through a Database-as-a-Queue model on PostgreSQL to eliminate dual-write hazards. A stateless Java API Service handles component decoupled task submissions, whilst a Python Worker Service implements distributed lease-locking and LangGraph state execution. The most non-trivial implementation challenge is ensuring the `PostgresDurableCheckpointer` prevents split-brain state corruption strictly via active database lease ownership checks during graph iteration.

#### A2. Impacted Components / Modules

  Component: Database Schema
  Change type: new code
  Path: `infrastructure/database/`
  Description: Implement the exact Phase 1 PostgreSQL schema and key query support described in `docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md`, including `tasks`, `checkpoints`, and `checkpoint_writes`, queue/reaper/dead-letter indexes, `updated_at` maintenance, and `LISTEN/NOTIFY` support for claimable task transitions.

  Component: API Service
  Change type: new code
  Path: `services/api-service/src/main/java/` and `services/api-service/src/main/resources/`
  Description: Build the Java Spring Boot REST API for the Phase 1 contract: task submission (`/v1/tasks`), status, checkpoint history, cancellation, redrive, dead-letter querying, and health (`/v1/health`) with strict request validation.

  Component: Worker Service Core
  Change type: new code
  Path: `services/worker-service/core/`
  Description: Implement the Python asyncio `FOR UPDATE SKIP LOCKED` task claim poller, background heartbeat loop, and distributed reaper.

  Component: Worker Service LangGraph Checkpointer
  Change type: new code
  Path: `services/worker-service/checkpointer/`
  Description: Implement the `PostgresDurableCheckpointer`, providing safety guarantees via lease validations during `put` transactions.

  Component: Co-located MCP Server
  Change type: new code
  Path: `services/worker-service/tools/`
  Description: Set up an in-process MCP server exposing MVP tool definitions for `web_search`, `read_url`, and `calculator`.

  Component: Worker Service Graph Executor
  Change type: new code
  Path: `services/worker-service/executor/`
  Description: Embed LangGraph `astream()` inside the Worker Service to translate API payloads into workflow states and manage tool dispatches. Cost and execution metadata are captured by Langfuse auto-instrumentation via its LangChain callback handler.

  Component: Demo Dashboard
  Change type: new code
  Path: `services/console/`
  Description: Build a React 19 + TypeScript SPA with Tailwind CSS/shadcn/ui that consumes the Phase 1 API endpoints. Customer-facing execution console providing task submission, live checkpoint timeline, cost and trace visualization (via Langfuse API), and dead letter queue management. Platform health is operator-only (CloudWatch).

  Component: AWS Cloud Infrastructure
  Change type: new code
  Path: `infrastructure/cdk/`, `services/api-service/`, `services/worker-service/`, `services/console/`, and `services/model-discovery/`
  Description: Provision foundational AWS resources using AWS CDK in TypeScript and implement application containerization assets required for deployment. This includes Docker build contexts for the API, Worker, and Console services, image packaging/publication strategy, a VPC, Aurora Serverless v2 PostgreSQL cluster, ECS Fargate services (API with CPU autoscaling, Console behind the same ALB, Worker with fixed instance count), an internal ALB reached through an SSM-managed access host, Model Discovery as a scheduled-and-initialized Lambda, imported Secrets Manager references, IAM execution/task roles, schema bootstrap with migration tracking, and CloudWatch integration. The edge layer should be structured so moving to a future public/customer-facing ALB is a contained change.

  Component: Langfuse Integration and Observability Split
  Change type: modification + new code
  Path: `services/worker-service/executor/`, `services/api-service/`, `services/console/`, `infrastructure/cdk/`, `infrastructure/database/migrations/`
  Description: Replace manual cost tracking with Langfuse auto-instrumentation of LangGraph. Deploy self-hosted Langfuse workloads plus the required backing services (PostgreSQL, Redis/Valkey, ClickHouse, or managed equivalents supported by Langfuse) behind the internal ALB. Add API Service proxy endpoints for Langfuse trace data. Update the Console to source cost/trace visualization from Langfuse. Separate customer-facing observability (Langfuse via Console) from operator-facing observability (CloudWatch metrics, dashboards, alarms). Remove `cost_microdollars` and `execution_metadata` from the checkpoints table. Export worker platform metrics to CloudWatch.

#### A3. Dependency Graph
All tasks are mostly independent except where schema or runtime contracts are shared:
  Task 1 (Database Schema) → depends on no prior tasks
  Task 2 (API Service) → depends on → Task 1 (Database Schema) for the exact Phase 1 schema and task/checkpoint query contract. Note: Task 2 validates `allowed_tools` against the Phase 1 tool set. Since the tool set is fixed (`web_search`, `read_url`, `calculator`), Task 2 hardcodes these as a compile-time constant rather than requiring a runtime dependency on Task 5.
  Task 3 (Worker Service Core) → depends on → Task 1
  Task 4 (LangGraph Checkpointer) → depends on → Task 1
  Task 5 (Co-located MCP Server) → depends on no prior tasks
  Task 6 (Worker Service Graph Executor) → depends on → Task 3, Task 4, Task 5
  Task 7 (Demo Dashboard) → depends on → Task 2 (API Service REST endpoints for data consumption)
  Task 8 (AWS Infrastructure and Containerization) → can run in parallel with all other tasks, but blocks final integration testing and deployment.
  Task 9 (Langfuse Integration and Observability Split) → depends on → Tasks 1–8 (all existing Phase 1 tasks must be complete). Modifies Worker (Task 6), API (Task 2), Console (Task 7), and Infrastructure (Task 8).

#### A4. Data / API / Schema Changes
  Change: Foundation PostgreSQL Schema setup
  Type: schema
  Backward compatible: yes (initial schema)
  Migration steps: None required (Greenfield application)

#### A4.1. Task Handoff Outputs
Each task should leave explicit artifacts for downstream consumers:

  Task 1 output
  DDL/migration files, schema tests, and a short schema README or comments identifying the canonical claim/reaper/checkpointer queries.

  Task 2 output
  Stable API DTOs, endpoint contracts, validation rules, and repository queries matching the Phase 1 schema.

  Task 3 output
  Reusable task-claim, heartbeat, reaper, and worker loop primitives that Task 6 can call without reimplementing queue semantics.

  Task 4 output
  A checkpointer package with a stable constructor contract, serialization behavior, and `LeaseRevokedException` semantics documented in code/tests.

  Task 5 output
  MCP `listTools` definitions and argument schemas that Task 2 and Task 6 can consume.

  Task 6 output
  A task execution entrypoint that accepts a claimed task record and performs graph execution and retry/dead-letter classification. Cost and execution metadata are captured by Langfuse auto-instrumentation.

  Task 7 output
  A production-ready React SPA with typed API client, live-polling checkpoint timeline, cost and trace visualization (via Langfuse API), and dead letter management, plus CORS configuration in the API Service.

  Task 8 output
  Deployable CDK stacks (Network, Data, Compute), API/Worker/Console container build assets (Dockerfiles and `.dockerignore` files), image publication wiring for ECS consumption, Console deployment behind the shared ALB, Model Discovery scheduled Lambda plus initial invocation, imported Secrets Manager references, schema bootstrap with migration tracking, and clear instructions for deploy/destroy workflow.

  Task 9 output
  Self-hosted Langfuse web/worker workloads plus the required PostgreSQL, Redis/Valkey, and ClickHouse backing services (or compatible managed equivalents). Worker Service using Langfuse callback handler instead of manual cost tracking (~150 lines removed from graph.py). API Service proxy endpoints for Langfuse trace/cost data using Langfuse Basic Auth. Console updated to source execution telemetry from Langfuse and stripped of platform health internals. Database migration removing cost columns from checkpoints. CloudWatch metric export, dashboard, and alarms for operator-facing platform health.

#### A5. Integration Points
  Caller: API Service
  Callee: PostgreSQL
  Interface change: JDBC payload mappings to the exact `tasks` and `checkpoints` schema, including tenant-scoped lookups, dead-letter listing, checkpoint history, and aggregate cost/checkpoint counts.
  Failure handling: Surface HTTP 5xx on persistent DB connection failure.

  Caller: Worker Service 
  Callee: PostgreSQL
  Interface change: `asyncpg` bindings for `LISTEN` queue polls, heartbeat persistence, and reaper scanning.
  Failure handling: Exponential backoff on database exception. Fallback to periodic polling if LISTEN/NOTIFY channels disconnect.

  Caller: Worker Service Graph Executor
  Callee: Provider LLM APIs (Bedrock/Anthropic)
  Interface change: HTTP Integration via `langchain` components.
  Failure handling: Retry backoff algorithm on transient issues (e.g. 429/5xx). Dead-letter transition on deterministic 4xx errors.

  Caller: Worker Service Graph Executor
  Callee: Co-located MCP Server
  Interface change: Local MCP Protocol JSON-RPC channel.
  Failure handling: Transient errors lead to task re-queue; validation errors trigger immediate dead-letter isolation.

#### A5.1. Dependency Pinning Checklist
Before implementation begins in earnest, the repo should pin or explicitly document these dependencies so task agents do not guess:

  API runtime
  Java version and Spring Boot major version

  Worker runtime
  Python version, `asyncpg`, LangGraph, LangChain, MCP Python library

  Checkpoint contract
  Exact LangGraph checkpoint package version and the `BaseCheckpointSaver` methods that must be implemented

  Infrastructure runtime
  Node.js/CDK versions and AWS CDK v2 package set for TypeScript

  Container build/runtime
  Base images, build tooling entrypoints, and image publication mechanism (for example CDK Docker assets or ECR push workflow) for the API and Worker services

#### A6. Deployment and Rollout Plan
  Infrastructure as Code: AWS CDK must be used to deploy all components. Manual AWS Console configuration or Terraform are prohibited.
  IaC language: TypeScript, matching the project-level stack decision.
  Containerization: API Service, Worker Service, and Console Service must each have a reproducible container build definition suitable for local verification and ECS deployment; container packaging must not be left implicit.
  Compute: ECS Fargate for API Service (Java), Worker Service (Python), and Console Service (containerized SPA served by nginx).
  Database: Amazon Aurora Serverless v2 (PostgreSQL).
  Networking: ECS services and VPC-attached Lambdas run in private subnets with NAT egress, Aurora runs in isolated subnets, and an internal ALB provides path-based routing for the API and Console. Operator access flows through an SSM-managed access host. The ALB/listener/target-group layout should remain reusable if a future public edge is introduced.
  Migration execution: Execute DB schema initialization independently via Infrastructure deployment tools (for example an AWS CDK Custom Resource). Schema logic must be strictly decoupled from Spring Boot/App startup and must use migration tracking rather than blind re-execution of all SQL files.
  Rollback trigger and steps: N/A

#### A7. Observability
  Logs: All logs mandate structural labels `task_id`, `worker_id`, and `node_name`. Key events logged MUST include `TASK_CLAIMED`, `NODE_STARTED`, `CHECKPOINT_SAVED`, `GRAPH_RESUMED`, `LEASE_REVOKED`, `TASK_COMPLETED`, and `TASK_DEAD_LETTERED`.
  Metrics (operator/CloudWatch): `tasks.submitted`, `tasks.active`, `tasks.dead_letter`, `workers.active_tasks`, `queue.depth`, `nodes.duration_ms`, `poll.empty`, `leases.expired`. Customer-facing cost and token metrics are captured by Langfuse and served via its REST API.
  Alerts: Trigger on dead letter accumulation (`tasks.dead_letter.count > 0` for > 5 min), lease expiry spikes (`leases.expired.rate > 10/min`).
  Dashboards: Customer-facing execution telemetry via Langfuse (cost, tokens, latency, traces) rendered in the Console. Operator-facing platform health via CloudWatch dashboards.

#### A8. Risks and Open Questions
  Technical risks: Divergent timeline state/billing artifacts if split-brain worker re-processes nodes simultaneously. Mitigated securely by Checkpointer verifying `lease_owner` before flushing payloads.
  Assumptions made:
  - ASSUMPTION: The API Service utilizes Java 21+ alongside Spring Boot 3+. — needs confirmation.
  - RESOLVED: The Worker Service utilizes Python 3.11+ (`requires-python = ">=3.11"` in pyproject.toml) and `asyncpg` for optimized database operations.
  - RESOLVED: LangGraph versions are pinned in Section 5.0 of the design doc: `langgraph==1.0.5`, `langgraph-checkpoint==4.0.0`, `langgraph-checkpoint-postgres==3.0.4`.
  - RESOLVED: The Console uses React 19 + TypeScript with Vite and is deployed in AWS as a containerized SPA behind the same ALB as the API for Phase 1.
  Open questions:
  - OPEN QUESTION: None currently blocking orchestration.

#### A9. Orchestrator Guidance
When assigning tasks to implementation agents:

  Use `docs/design/phase-1/PHASE1_DURABLE_EXECUTION.md` as the canonical architecture/behavior contract and `agent_tasks/*.md` as the task-local implementation contract.

  Require each agent to state any dependency/version assumption explicitly before coding if that assumption is not already pinned in the repo.

  Prefer landing Task 1 first or at least validating its schema contract before Task 2, Task 3, and Task 4 are merged.

  Treat Task 5 as an interface provider for both Task 2 validation (`listTools`) and Task 6 tool dispatch.

  Keep `plan.md` focused on sequencing, handoff expectations, rollout, and governance. Do not rely on it as the only place for task-critical implementation details.

  Do not accept implementations that introduce new statuses, new dead-letter reasons, alternate retry semantics, or different infrastructure choices than the Phase 1 design without first updating the design doc.

---

### SECTION B — Agent Task Files

The agent task specifications have been split into standalone files for easier parallel execution. 
Please refer to the following tasks in the `agent_tasks/` directory:

- [Task 1: Database Schema](./agent_tasks/task-1-database-schema.md)
- [Task 2: API Service REST Endpoints](./agent_tasks/task-2-api-service.md)
- [Task 3: Worker Service Core](./agent_tasks/task-3-worker-service-core.md)
- [Task 4: LangGraph Postgres Checkpointer](./agent_tasks/task-4-langgraph-checkpointer.md)
- [Task 5: Co-located MCP Server](./agent_tasks/task-5-mcp-server.md)
- [Task 6: Graph Executor Assembly](./agent_tasks/task-6-graph-executor.md)
- [Task 7: Console](./agent_tasks/task-7-console.md)
- [Task 8: AWS Cloud Infrastructure](./agent_tasks/task-8-aws-infrastructure.md)
- [Task 9: Langfuse Integration and Observability Split](./agent_tasks/task-9-langfuse-observability.md)

Tracking of these tasks can be found in [progress.md](./progress.md).

---

### SECTION C — Task 8 Progress Tracking

**Status (2026-03-19):** Implementation complete and ready for AWS account validation.

Completed work:
- CDK app scaffolded under `infrastructure/cdk/` with Network, Data, and Compute stacks.
- Aurora Serverless v2, imported Secrets Manager references, schema bootstrap, ECS services, internal ALB, access host, and Model Discovery scheduling/initial invocation wired.
- Service-owned Dockerfiles and `.dockerignore` files added for API, Worker, Console, and Model Discovery packaging.
- Worker and console runtime compatibility updated for split DB env vars and same-origin production API requests.
- Infrastructure README and CDK assertion coverage added.

Verification:
- Service-side console build and Python syntax/import checks passed locally.
- Live AWS bootstrap/deploy remains pending AWS credentials and account access.
