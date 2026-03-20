<!-- AGENT_TASK_START: task-8-aws-infrastructure.md -->

# Task 8: AWS Cloud Infrastructure

## Agent Instructions
You are a software engineer or cloud architect implementing the Infrastructure as Code (IaC) for a larger system.
Your scope is strictly limited to this task. Do not modify application behavior outside
the "Affected Component" listed below, except for small deployment-enabling changes explicitly called out here
(for example Dockerfiles, console production API base handling, or worker startup env compatibility).

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files to understand the system architecture and constraints:
1. `docs/PROJECT.md`
2. `docs/design/PHASE1_DURABLE_EXECUTION.md`

**CRITICAL POST-WORK:** After completing this task, you MUST update the status of this task to "Done" in the `docs/implementation_plan/phase-1/progress.md` file.

**DEPLOYMENT ACCESS RULE:** If you reach the point of performing a real AWS bootstrap, deploy, destroy, or any command that requires live AWS account access, you MUST stop and ask the user for AWS credentials/configured account access first. Local CDK synthesis, unit tests, Docker builds, and static template validation may proceed without that prompt.

## Context
The Phase 1 Persistent Agent Runtime is designed as a cloud-native AWS deployment centered on Aurora Serverless v2 PostgreSQL, ECS Fargate, and Lambda. The API Service and Worker Service are the primary runtime components. The Console is hosted as a containerized SPA behind the same ALB as the API, so the browser can use same-origin requests with no application-layer authentication. For this MVP, the ALB is **internal**, and operator access is provided through an SSM-managed access host (jump host) used for port forwarding into the VPC.

This task also owns the container packaging assets needed to build runnable images for the API Service, Worker Service, and Console Service.

## Task-Specific Shared Contract
- Treat `docs/PROJECT.md` and `docs/design/PHASE1_DURABLE_EXECUTION.md` as the canonical infrastructure direction. This task should not reopen Phase 1 architecture choices already made.
- Phase 1 has **no application-layer authentication**. Do not add password filters, fake login flows, or browser-exposed shared secrets. Restrict access through private networking and the SSM-managed access host instead.
- Infrastructure choices are fixed for this task: AWS CDK in TypeScript, Aurora Serverless v2 PostgreSQL, ECS Fargate, private subnets for compute, isolated subnets for Aurora, one internal ALB, one NAT gateway, one small SSM-managed access host, CloudWatch integration, and Secrets Manager for sensitive values.
- Schema initialization must remain decoupled from service startup.
- The Console must be deployed as a containerized static site behind the same ALB as the API using path-based routing, not CloudFront.
- Secrets must be referenced from **pre-created AWS Secrets Manager secrets**. Do not read raw secret values from local environment variables at CDK synth time and materialize them into templates.
- Imported Secrets Manager secret payload shapes must be explicit and consistent:
  - `tavilySecretName`, `anthropicSecretName`, and `openaiSecretName` must each be stored as a plaintext secret string containing only the API key
  - Do not support multiple secret payload shapes for the same input in Phase 1; keep the operator contract simple and unambiguous
- The schema bootstrap must use migration tracking. Do not re-run all SQL files blindly on every deployment because the current migration files are not idempotent.
- The Worker Service must not depend on LLM provider secrets at runtime; those provider keys belong to the Model Discovery flow and the database-backed `provider_keys` table.
- The Worker Service startup path must be brought into alignment with that contract: remove or revise any startup validation/logging that treats missing `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` as a worker misconfiguration.
- Structure the load balancer and routing code so a future move to a public customer-facing ALB is isolated to the edge layer (ALB scheme, subnets, ingress, and optional auth/WAF), without changing API/Console/Worker task definitions or target-group routing.

## Affected Component
- **Service/Module:** AWS Cloud Infrastructure
- **File paths (if known):** `infrastructure/cdk/`, `services/api-service/`, `services/worker-service/`, `services/console/`, `services/model-discovery/`
- **Change type:** new code

## Dependencies
- **Must complete first:** None (can be built in parallel with code tasks, but coordinate with application owners if packaging inputs or startup commands are unclear)
- **Provides output to:** Final deployment/integration pipeline and end-to-end AWS demo environment
- **Shared interfaces/contracts:** VPC, ALB routing, access-host connectivity, security groups, Secrets Manager references, and IAM role definitions dictating exactly what the API, Worker, Console, and Lambda components are allowed to access

---

## Decision Log

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Single environment, prefixed stack names | MVP cost savings; future phases can add new stack sets with a different `envName` |
| 2 | 1 NAT Gateway | Lowest-cost managed option that still lets private ECS tasks and Lambdas reach provider APIs |
| 3 | Schema bootstrap via Lambda Custom Resource with migration ledger | Fully automated, safe for repeat deploys, and compatible with non-idempotent SQL migration files |
| 4 | CDK `DockerImageAsset` for API, Worker, and Console images | Simplest Phase 1 packaging path without introducing a separate image pipeline |
| 5 | Console runs on ECS behind the same ALB as the API | Avoids CloudFront/private-access mismatch and avoids build-time ALB URL injection |
| 6 | Network-layer access control only | Phase 1 design explicitly defers auth; operator access is via an SSM-managed access host and an internal ALB |
| 7 | Sensitive values come from pre-created Secrets Manager secrets | Prevents secrets from leaking into synthesized templates or local shell history |
| 8 | Model Discovery is both scheduled and invoked once at deploy time | Prevents a fresh stack from having an empty `models` table and failing task submission |
| 9 | Worker fixed desired count, no autoscaling | Worker is `asyncio` single-threaded and I/O-bound; CPU metrics do not reflect queue pressure |
| 10 | Keep the edge layer swappable | Future public expansion should mainly change ALB exposure/auth at the edge, not the app/service layout behind it |

---

## Stack Architecture

Three stacks with explicit cross-stack references:

```
┌─────────────────────────────────────────────────────────────┐
│  PersistentAgentRuntime-{envName}-Network                   │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ VPC (2 AZs)                                         │    │
│  │  ├── Public Subnets (NAT + SSM access host)         │    │
│  │  ├── Private Subnets w/ egress (ALB + ECS + Lambda) │    │
│  │  └── Isolated Subnets (Aurora)                      │    │
│  │ Security Groups: AccessHost, ALB, API, Console,     │    │
│  │                  Worker, DB, Lambda                 │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  PersistentAgentRuntime-{envName}-Data                      │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Aurora Serverless v2 (PostgreSQL 16)                │    │
│  │  ├── Min 0.5 ACU / Max 4 ACU                        │    │
│  │  ├── Isolated subnets only                          │    │
│  │  └── Credentials → Secrets Manager (auto-generated) │    │
│  │ Schema Bootstrap Lambda + Custom Resource           │    │
│  │ Imported external API key secrets                   │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  PersistentAgentRuntime-{envName}-Compute                   │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ ECS Cluster (Fargate)                               │    │
│  │  ├── API Service (Spring Boot, port 8080)           │    │
│  │  ├── Console Service (nginx, port 80)               │    │
│  │  └── Worker Service (Python, no inbound)            │    │
│  │ Internal ALB (reachable from access host only)      │    │
│  │  ├── /v1/*   → API target group                     │    │
│  │  └── /*      → Console target group                 │    │
│  │ Model Discovery Lambda (scheduled + initial run)    │    │
│  │ SSM Access Host (port-forward entry point)          │    │
│  │ IAM: least-privilege task and function roles        │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

---

## Implementation Steps

### Step 0: CDK Project Bootstrap

**Goal:** Initialize the CDK TypeScript project and deployment inputs.

- Run `cdk init app --language typescript` in `infrastructure/cdk/`
- Configure `cdk.json` with context parameters:
  - `envName` (default: `dev`)
  - `workerDesiredCount` — default `1`
  - `tavilySecretName` — optional existing Secrets Manager secret name or ARN
  - `anthropicSecretName` — optional existing Secrets Manager secret name or ARN
  - `openaiSecretName` — optional existing Secrets Manager secret name or ARN
  - `accessHostInstanceType` — default `t3.micro`
- Design the CDK stack interfaces so the ALB exposure mode can be changed later with minimal code churn. The default implementation here is internal-only, but the load balancer construction should be isolated enough that a future internet-facing mode is a targeted edge change.
- Add standard CDK v2 dependencies plus any Lambda packaging helpers actually needed
- Set up `bin/app.ts` instantiating the 3 stacks with explicit cross-stack references
- Add billing tags (`Project: PersistentAgentRuntime`, `Environment: {envName}`) to all stacks via `cdk.Tags.of(app).add()`

**Files created:**
- `infrastructure/cdk/bin/app.ts`
- `infrastructure/cdk/lib/network-stack.ts`
- `infrastructure/cdk/lib/data-stack.ts`
- `infrastructure/cdk/lib/compute-stack.ts`
- `infrastructure/cdk/cdk.json`
- `infrastructure/cdk/tsconfig.json`
- `infrastructure/cdk/package.json`

---

### Step 1: Network Stack

**Goal:** VPC, subnets, NAT, and security groups.

**VPC:**
- 2 Availability Zones
- 3 subnet tiers:
  - `Public` — NAT Gateway and the SSM-managed access host
  - `Private with egress` — internal ALB, ECS Fargate tasks, and VPC-attached Lambdas
  - `Isolated` — Aurora cluster
- 1 NAT Gateway (`natGateways: 1`)

**Security Groups:**
- `accessHostSg`
  - Inbound: none (SSM only; no SSH ingress)
  - Outbound: to ALB SG on 80 and to AWS control plane via normal egress
- `albSg`
  - Inbound: TCP 80 from `accessHostSg` only
  - Outbound: to API SG on 8080 and Console SG on 80
- `apiServiceSg`
  - Inbound: TCP 8080 from ALB SG only
  - Outbound: to DB SG on 5432 and HTTPS 443 to AWS services (ECR image pull, CloudWatch Logs, Secrets Manager) via NAT
- `consoleServiceSg`
  - Inbound: TCP 80 from ALB SG only
  - Outbound: HTTPS 443 to AWS services (ECR image pull, CloudWatch Logs) via NAT
- `workerServiceSg`
  - Inbound: none
  - Outbound: to DB SG on 5432 and to the internet via NAT for tool/provider access
- `dbSg`
  - Inbound: TCP 5432 from API SG, Worker SG, and Lambda SG
  - Outbound: none
- `lambdaSg`
  - Inbound: none
  - Outbound: to DB SG on 5432 and to the internet via NAT for provider API discovery

**Access host network placement** (the EC2 resource itself is created in Step 4 / Compute Stack):
- Placed in a public subnet with a public IPv4 address so SSM Session Manager can reach AWS service endpoints without adding separate VPC interface endpoints for this MVP
- SSM agent enabled; no public inbound security group rules; no SSH key required
- Used with Session Manager remote-host port forwarding to reach the internal ALB from the operator laptop

**Exports:** VPC, security groups, subnet selections

---

### Step 2: Data Stack

**Goal:** Aurora Serverless v2 cluster, imported Secrets Manager secrets, and schema bootstrap.

**Aurora Serverless v2:**
- Engine: PostgreSQL 16 compatible
- Writer: serverless v2, min 0.5 ACU / max 4 ACU
- Subnet group: isolated subnets
- Security group: `dbSg`
- Credentials: auto-generated by CDK in Secrets Manager
- Database name: `persistent_agent_runtime`
- Deletion protection: off for MVP (configurable)
- Backup retention: 7 days
- No public accessibility

**Imported external secrets:**
- Import, do not create from local env vars:
  - `tavilySecretName` — optional
  - `anthropicSecretName` — optional
  - `openaiSecretName` — optional
- Use `secretsmanager.Secret.fromSecretNameV2` or ARN equivalent
- Each imported secret must be a plaintext secret value containing the API key
- Wire imported secrets into runtime env vars using the mechanism appropriate to each compute type:
  - Worker (ECS): `TAVILY_API_KEY` via `ecs.Secret.fromSecretsManager(tavilySecret)` (no field selector — plaintext secret)
  - Model Discovery (Lambda): `ANTHROPIC_API_KEY_SECRET_ARN` and `OPENAI_API_KEY_SECRET_ARN` as plain env vars containing the secret ARN, resolved at runtime via Secrets Manager SDK (Lambda does not support ECS-style secret injection)
- If `tavilySecretName` is omitted, the Worker Service must still deploy successfully and `web_search` should be unavailable until the secret is configured
- If `anthropicSecretName` and/or `openaiSecretName` are omitted, Model Discovery should still deploy and no-op for the missing providers

**Schema Bootstrap Lambda (Custom Resource):**
- Runtime: Python 3.12 or Node.js 20. If Python with `psycopg` is chosen, use `DockerImageFunction` for the same native dependency reason as Model Discovery. Node.js with `pg` (pure JS) avoids this issue and works as a standard zip Lambda.
- Migration SQL files from `infrastructure/database/migrations/` must be bundled into the Lambda deployment artifact (e.g., `COPY` into the Docker image or included in the zip bundle). The Lambda reads these bundled files in lexical order, filtering to only files matching `^\d{4}_.*\.sql` (e.g., `0001_phase1_durable_execution.sql`). This excludes non-migration files such as `test_seed.sql` which exist in the same directory for e2e testing purposes.
- Before applying migrations, ensures a migration ledger table exists:
  - `schema_migrations(filename text primary key, checksum text not null, applied_at timestamptz not null default now())`
- Applies only unapplied migrations
- Records filename + checksum after each successful migration
- If a migration filename already exists in `schema_migrations` with a different checksum than the bundled file, fail the deployment rather than silently continuing
- Environment variables:
  - `DB_CREDENTIALS_SECRET_ARN` — ARN of the Aurora auto-generated credentials secret (the JSON payload contains `host`, `port`, `dbname`, `username`, and `password`)
- **Secret resolution:** Lambda does not support ECS-style secret field injection. At runtime, use the Secrets Manager SDK (`GetSecretValue`) to fetch the Aurora credentials JSON and parse out connection parameters. This is the same pattern used by Model Discovery.
- Runs inside the VPC in private-with-egress subnets with `lambdaSg`
- IAM:
  - Read access to Aurora credentials secret
  - VPC network interface permissions
  - CloudWatch Logs write permissions (for example `AWSLambdaBasicExecutionRole`)
- CloudFormation lifecycle behavior:
  - `Create` / `Update`: apply unapplied migrations only
  - `Delete`: return success without attempting destructive schema teardown. The Delete handler must catch and swallow **all** exceptions (connection errors, timeouts, DNS failures, etc.) because during `cdk destroy --all` the Aurora cluster or VPC may already be deleted by the time this handler runs. Any unhandled error will cause CloudFormation to hang for up to an hour waiting for a retry.

**Important:** Do **not** implement a `migrationVersion` knob that re-runs all SQL files. The current migration files are not safely repeatable.

**Exports:**
- Aurora endpoint
- Aurora port
- Database name
- Aurora credentials secret ARN
- Imported external secret references needed by the Compute stack

---

### Step 3: Containerization Assets

**Goal:** Production container images for API, Worker, and Console.

This task owns explicit container packaging. Do not leave build contexts or entrypoints implicit.

**API Service Dockerfile**
- Create `services/api-service/Dockerfile`
- Multi-stage build using Java 21
- Produces a Spring Boot runnable jar
- Exposes port 8080

**Worker Service Dockerfile**
- Create `services/worker-service/Dockerfile`
- Python 3.11 slim base
- Copy the full project source before installing the local package so packaging succeeds
- Entrypoint should run the worker process exactly as used in local development

**Console Service Dockerfile**
- Create `services/console/Dockerfile`
- Multi-stage:
  - Build stage with Node.js
  - Runtime stage with nginx serving static assets
- Include an nginx config that:
  - Serves the built SPA
  - Uses `try_files` to return `index.html` for client-side routes
  - Exposes a simple `/healthz` endpoint returning `200`

**Docker ignore files**
- Add `.dockerignore` files for API, Worker, and Console to avoid sending tests, local caches, and docs into image builds

**Small deployment-enabling app changes permitted in this task**
- Update `services/console/` so production can use same-origin API requests
- The Console should not require a baked-in ALB hostname
- Prefer:
  - local dev: `VITE_API_BASE_URL=http://localhost:8080`
  - deployed via SSM tunnel: `VITE_API_BASE_URL=` empty or omitted, with the client treating that as same-origin
- Keep local dev behavior intact

---

### Step 4: Compute Stack — ECS Cluster, Internal ALB, and Access Host

**Goal:** Shared ECS cluster, internal ALB, access host, and routing for API + Console.

**ECS Cluster:**
- Cluster name: `par-{envName}`
- Container Insights enabled
- CloudWatch scope for this task:
  - Required: CloudWatch Logs for all ECS tasks and Lambdas, plus ECS Container Insights on the cluster
  - Optional: CloudWatch dashboards and alarms, unless the application already emits the required metrics in a form that can be wired without inventing new instrumentation in this task

**Application Load Balancer:**
- Scheme: internal
- Subnets: private with egress
- Security group: `albSg`
- Listener: HTTP 80 only for MVP
- Target groups:
  - `apiTargetGroup` → API Service on port 8080 (`targetType: ip` for Fargate)
  - `consoleTargetGroup` → Console Service on port 80 (`targetType: ip` for Fargate)
- Routing:
  - `/v1/*` → API target group
  - default `/*` → Console target group
- Health checks:
  - API target group: `GET /v1/health`
  - Console target group: `GET /healthz`

**Access host:**
- Small EC2 instance using SSM Session Manager for access
- IAM instance profile with `AmazonSSMManagedInstanceCore` managed policy (required for SSM Session Manager to function)
- No public SSH exposure
- Document the expected access workflow in the README:
  - operator starts an `AWS-StartPortForwardingSessionToRemoteHost` session through the access host to the internal ALB DNS name
  - browser opens the forwarded local port

**Future expansion note:** Keep the ALB listeners, target groups, and path routing independent from the access-host logic. Later customer expansion should be able to swap the ALB to internet-facing and replace the access-host pattern without reworking the service topology behind the ALB.

**Important:** Do not add a password gate in the API or browser.

**Exports:** Access host instance ID, internal ALB DNS name

---

### Step 5: Compute Stack — API Service (ECS Fargate)

**Goal:** Fargate service for the Spring Boot API.

**Task Definition:**
- CPU: 512
- Memory: 1024 MB
- Container image: `DockerImageAsset` from `services/api-service/`
- Port mapping: 8080
- Log driver: `awslogs`
- Environment variables:
  - `DB_HOST`
  - `DB_PORT`
  - `DB_NAME`
  - `SERVER_PORT=8080`
- Secrets (extracted from Aurora auto-generated Secrets Manager JSON using `ecs.Secret.fromSecretsManager(secret, 'username')` / `'password'` field selectors):
  - `DB_USER`
  - `DB_PASSWORD`

**Fargate Service:**
- Desired count: 1
- Subnets: private with egress
- Security group: `apiServiceSg`
- Attached to `apiTargetGroup`
- Health check grace period: 120s

**Auto-scaling:**
- Min: 1
- Max: 4
- Target tracking: CPU utilization at 60%

**IAM:**
- Execution role:
  - `AmazonECSTaskExecutionRolePolicy`
  - Read access to Aurora credentials secret
- Task role:
  - minimal additional permissions only

**Do not add:** password auth filter, shared secret header checks, or any browser-facing credential requirement

---

### Step 6: Compute Stack — Console Service (ECS Fargate)

**Goal:** Fargate service for the React SPA served by nginx.

**Task Definition:**
- CPU: 256
- Memory: 512 MB
- Container image: `DockerImageAsset` from `services/console/`
- Port mapping: 80
- Log driver: `awslogs`
- No runtime secrets

**Fargate Service:**
- Desired count: 1
- Subnets: private with egress
- Security group: `consoleServiceSg`
- Attached to `consoleTargetGroup`

**Runtime behavior:**
- SPA is served at `/`
- Browser calls the API through the same ALB using same-origin requests
- Keep existing local-dev CORS support for `localhost`, but the deployed AWS path should not depend on CORS

---

### Step 7: Compute Stack — Worker Service (ECS Fargate)

**Goal:** Fargate service for the Python worker.

**Task Definition:**
- CPU: 1024
- Memory: 2048 MB
- Container image: `DockerImageAsset` from `services/worker-service/`
- No port mapping
- Log driver: `awslogs`
- Environment variables:
  - `DB_HOST`
  - `DB_PORT`
  - `DB_NAME`
- Secrets:
  - `DB_USER`
  - `DB_PASSWORD`
  - `TAVILY_API_KEY` when `tavilySecretName` is configured

**Worker DB compatibility requirement:**
- The current worker runtime expects `DB_DSN`
- This task must make ECS deployment compatible in one of these clean ways:
  - Preferred: update worker startup code to accept split `DB_*` values and construct `DB_DSN` if it is absent
  - Acceptable alternative: use the container command/entrypoint to compose `DB_DSN` from injected `DB_*` values before launching the process
- Do not inject Anthropic/OpenAI secrets into the worker task; the worker resolves provider keys from the database
- Update the worker startup path and docs so missing `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are not logged as warnings or treated as required in the deployed runtime

**Fargate Service:**
- Desired count: value from `workerDesiredCount` (default 1)
- Subnets: private with egress
- Security group: `workerServiceSg`
- No load balancer

**Scaling (Phase 1):**
- Fixed desired count only
- No autoscaling

**IAM:**
- Execution role:
  - `AmazonECSTaskExecutionRolePolicy`
  - Read access to Aurora credentials secret
  - Read access to Tavily secret
- Task role:
  - minimal permissions only unless a clearly required runtime integration needs more

---

### Step 8: Model Discovery Lambda (Scheduled + Initial Run)

**Goal:** Keep the `models` and `provider_keys` tables synchronized with configured LLM provider secrets.

**Lambda packaging:**
- Package `services/model-discovery/` as a Docker-based Lambda (`DockerImageFunction`) because `psycopg[binary]` contains native C extensions that require an Amazon Linux-compatible build. A simple zip bundle will not work.
- Add a proper Lambda handler entrypoint; do not rely on the current CLI-only `__main__` flow

**Lambda configuration:**
- Runtime: Python 3.12 (via Docker base image `public.ecr.aws/lambda/python:3.12`)
- Timeout: 120s
- Memory: 256 MB
- VPC-attached in private-with-egress subnets using `lambdaSg`
- Environment variables:
  - `DB_HOST`
  - `DB_PORT`
  - `DB_NAME`
  - `DB_CREDENTIALS_SECRET_ARN` — ARN of the Aurora auto-generated credentials secret
  - `ANTHROPIC_API_KEY_SECRET_ARN` — ARN of the imported Anthropic secret (omit if not configured)
  - `OPENAI_API_KEY_SECRET_ARN` — ARN of the imported OpenAI secret (omit if not configured)
- **Secret resolution strategy:** Lambda does not support ECS-style `Secret` field injection. Instead, pass secret ARNs as plain environment variables and resolve the actual values at runtime using the Secrets Manager SDK (`GetSecretValue`). The handler must:
  - Parse the Aurora credentials JSON to extract `username` and `password`
  - Read each provider API key secret as a plaintext string
  - Handle missing optional ARN env vars gracefully (skip that provider)

**Behavior:**
1. Read configured provider secrets
2. Discover available models from each configured provider
3. Upsert `provider_keys` for currently configured providers
4. Remove or invalidate `provider_keys` rows for providers that are no longer configured in this environment so runtime model resolution cannot continue using stale credentials. **Known MVP limitation:** if a task is currently in-flight using a model from a removed provider, subsequent LLM calls within that task will fail. This is acceptable for Phase 1; a future improvement could mark keys as "draining" with a TTL instead of immediate removal.
5. Upsert active models and pricing into `models`
6. Mark models inactive for providers not discovered in the current run
7. If no provider secrets are configured, succeed as a no-op with clear logs

**Scheduling:**
- EventBridge rule with `rate(1 day)`

**Initial run requirement:**
- A fresh environment must not wait for the first scheduled run
- After schema bootstrap is complete, invoke Model Discovery once automatically via a deployment-time custom resource or equivalent one-time infrastructure-triggered invocation
- **Dependency ordering:** The initial invocation custom resource must have an explicit CDK dependency (`node.addDependency()`) on the schema bootstrap custom resource to guarantee migrations complete before Model Discovery writes to the `models` and `provider_keys` tables. Without this, CloudFormation may execute them in parallel and the invocation will fail against an uninitialized schema.
- The deployment-time invocation resource must be CloudFormation-lifecycle safe:
  - `Create` / `Update`: invoke once after schema bootstrap
  - `Delete`: return success without invoking the function. The Delete handler must catch and swallow all exceptions (including Lambda-not-found errors) because the Model Discovery Lambda may already be deleted during stack teardown.

**IAM:**
- Read access to Aurora credentials secret
- Read access to optional provider secrets
- VPC network interface permissions
- CloudWatch Logs write permissions (for example `AWSLambdaBasicExecutionRole`)

---

### Step 9: CDK Unit Tests

**Goal:** Validate synthesized templates using CDK `Assertions`.

**Test file:** `infrastructure/cdk/test/stacks.test.ts`

**Assertions to verify:**
- Network stack:
  - VPC has 2 AZs
  - Exactly 1 NAT Gateway
  - 7 security groups exist
- Data stack:
  - Aurora cluster exists with PostgreSQL engine
  - Cluster is not publicly accessible
  - Schema bootstrap custom resource exists
- Compute stack:
  - ECS cluster exists
  - 3 Fargate services exist (API, Console, Worker)
  - Internal ALB exists
  - Listener rule routes `/v1/*` to the API target group
  - Default listener action routes to the Console target group
  - Worker service has no port mappings and no autoscaling policy
  - API service has CPU-based autoscaling
  - Model Discovery Lambda exists with EventBridge schedule
  - Initial model discovery invocation resource exists
  - All resources are tagged with `Project: PersistentAgentRuntime`

---

### Step 10: Infrastructure README

**Goal:** `infrastructure/README.md` documenting synth, deploy, destroy, and secret setup.

**Contents:**
1. Prerequisites
   - AWS CLI
   - AWS Session Manager plugin
   - CDK CLI
   - Docker
   - Node.js
2. AWS credentials setup
3. Access workflow
   - use SSM Session Manager `AWS-StartPortForwardingSessionToRemoteHost` through the access host to reach the internal ALB
   - note that the access host uses a public IP and still has no inbound SSH or HTTP exposure
4. Required pre-created Secrets Manager secrets
   - Tavily secret (optional)
   - Anthropic secret (optional)
   - OpenAI secret (optional)
   - supported payload shape: plaintext secret string only
5. CDK context parameters
   - `envName` (default `dev`)
   - `workerDesiredCount` (default `1`)
   - `accessHostInstanceType` (default `t3.micro`)
   - `tavilySecretName` (optional)
   - `anthropicSecretName` (optional)
   - `openaiSecretName` (optional)
6. Commands
   - `cdk synth`
   - `cdk deploy --all`
   - `cdk deploy --all -c tavilySecretName=par/dev/tavily_api_key` (optional: enables `web_search`)
   - `cdk destroy --all`
7. Access model
   - ALB is internal-only
   - access is via SSM-managed port forwarding through the access host
   - no application password required
8. Schema bootstrap notes
   - migrations are tracked in `schema_migrations`
9. Model Discovery notes
   - initial run happens automatically after deploy
   - scheduled sync runs daily afterward
10. Cost estimate

---

## Deploy-Time Input Summary

| Input | Source | Required | Purpose |
|-------|--------|----------|---------|
| `envName` | CDK context (`-c`) | No (default: `dev`) | Stack name prefix |
| `workerDesiredCount` | CDK context (`-c`) | No (default: `1`) | Worker instance count |
| `accessHostInstanceType` | CDK context (`-c`) | No (default: `t3.micro`) | SSM-managed access host sizing |
| `tavilySecretName` | CDK context (`-c`) | No | Existing Secrets Manager secret for worker `web_search` tool |
| `anthropicSecretName` | CDK context (`-c`) | No | Existing Secrets Manager secret for Model Discovery |
| `openaiSecretName` | CDK context (`-c`) | No | Existing Secrets Manager secret for Model Discovery |
| AWS credentials | `~/.aws/credentials` or equivalent | Yes | CDK deployment target account |

Secret payload contract:
- All three imported API-key secrets must be stored as plaintext secret values.
- JSON-shaped Secrets Manager payloads are out of scope for Phase 1.

---

## Execution Order

```
Step 0: CDK project init
Step 3: Dockerfiles and small deployment-enabling app changes
   ↓
Step 1: Network Stack
   ↓
Step 2: Data Stack (depends on Network)
   ↓
Steps 4-7: Compute Stack — ALB + ECS services (depends on Network + Data)
Step 8: Model Discovery Lambda + initial invocation (depends on Data)
   ↓
Step 9: CDK unit tests
Step 10: README
```

The schema bootstrap must complete before the initial Model Discovery invocation. The Console no longer depends on a provisioned ALB hostname at build time because it uses same-origin API calls behind the shared ALB.

---

## Cost Estimate (MVP / dev)

| Resource | Monthly Cost (approx) |
|----------|-----------------------|
| Aurora Serverless v2 (0.5 ACU min, mostly idle) | ~$22 |
| NAT Gateway (1x) + data processing | ~$35 |
| ECS Fargate — API (0.5 vCPU, 1GB, 1 task) | ~$15 |
| ECS Fargate — Console (0.25 vCPU, 0.5GB, 1 task) | ~$8 |
| ECS Fargate — Worker (1 vCPU, 2GB, 1 task) | ~$30 |
| ALB | ~$16 |
| CloudWatch Logs | ~$5 |
| Lambda (schema bootstrap + model discovery) | <$1 |
| EC2 access host (t3.micro, 24/7) | ~$8 |
| Secrets Manager | depends on number of imported secrets |
| **Total** | **~$135/month plus Secrets Manager and outbound API traffic** |

---

## Future Multi-Stage Expansion

When ready to add staging/prod:
1. Pass `envName=staging` or `envName=prod`
2. This creates entirely new stack sets
3. No existing `dev` resources are touched
4. Adjust ACU limits, NAT gateway count, and desired counts per environment
5. Move to a dedicated image pipeline and CDK Pipelines in a later phase

---

## Acceptance Criteria
The implementation is complete when:
- [ ] Valid AWS CDK infrastructure code is committed under `infrastructure/cdk/`
- [ ] API, Worker, and Console each have explicit container build definitions suitable for ECS deployment
- [ ] The Console is deployed as an ECS service behind the same ALB as the API
- [ ] ALB path-based routing sends `/v1/*` to the API and `/*` to the Console
- [ ] Access is provided through an SSM-managed access host to an internal ALB; no app-level password auth is added
- [ ] All external API keys are sourced from pre-created Secrets Manager secrets
- [ ] AWS deployment succeeds without `tavilySecretName`; in that case only the `web_search` tool is unavailable
- [ ] Schema bootstrap executes migrations in order using a migration ledger and does not blindly re-run all SQL files
- [ ] Model Discovery is deployed as a scheduled Lambda and is also invoked automatically once after deploy
- [ ] Custom resources for schema bootstrap and initial model discovery are safe on CloudFormation `Delete` events and do not block `cdk destroy --all`
- [ ] Imported Tavily/Anthropic/OpenAI secrets have a documented plaintext payload contract and are wired predictably into runtime env vars
- [ ] Model Discovery removes or invalidates stale provider credentials when a provider secret is no longer configured
- [ ] Worker Service uses a fixed desired count with no autoscaling; API Service uses CPU-based autoscaling
- [ ] Worker startup no longer treats missing LLM provider env vars as a runtime problem in AWS
- [ ] `infrastructure/README.md` explains secret setup, synth, deploy, destroy, and access model

## Testing Requirements
- **Unit tests:** Use CDK `Assertions` to validate synthesized templates
- **Build verification:** Build the API, Worker, Console, and Docker-based Model Discovery Lambda images locally to confirm Dockerfiles and build contexts are correct
- **Integration tests:** Optional real deployment to a sandbox AWS account only after explicitly requesting AWS access from the user

## Constraints and Guardrails
- **DO NOT** make the database publicly accessible
- **DO NOT** add fake login, shared browser password, or header-based API auth
- **DO NOT** read raw secret values from local env vars during CDK synth and convert them into CloudFormation resources
- **DO NOT** blindly re-run existing SQL migrations on every deploy
- **DO NOT** inject Anthropic/OpenAI secrets into the Worker task unless the worker runtime contract is explicitly changed and documented
- Ensure all ECS tasks and Lambdas export logs to CloudWatch automatically
- Treat CloudWatch Logs plus ECS Container Insights as the minimum required CloudWatch integration for this task; dashboards/alarms are optional follow-up work unless explicitly implemented from existing emitted metrics
- Keep the ALB/listener/target-group structure decoupled from the access-host implementation so a future public ALB rollout is localized to the edge layer

## Assumptions / Open Questions for This Task
- ASSUMPTION: For the MVP, HTTP over an operator-initiated SSM port-forwarding session to the internal ALB is acceptable. If HTTPS, public exposure, or customer-facing auth is required later, add that as a focused edge-layer follow-up rather than changing the service layout behind the ALB.

<!-- AGENT_TASK_END: task-8-aws-infrastructure.md -->
