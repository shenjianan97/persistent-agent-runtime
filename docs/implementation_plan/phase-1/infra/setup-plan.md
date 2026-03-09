# Phase 1 — AWS Infrastructure Setup Plan

## Overview

This plan covers the full AWS CDK infrastructure for Phase 1 of the Persistent Agent Runtime. It provisions networking, database, compute (ECS Fargate), observability, and security components — plus the Dockerfiles and a small API-service code change for password authentication.

**IaC tool:** AWS CDK (TypeScript)
**Region:** User-specified at deploy time via `AWS_REGION` / CDK context
**Environment:** Single environment for MVP; stack naming uses a configurable `envName` prefix (default `dev`) so future stages can be added without recreating resources

---

## Decision Log

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Single environment, prefixed stack names | MVP cost savings; future stages add new stacks with different prefix |
| 2 | 1 NAT Gateway | ~$32/month savings; sufficient for MVP traffic |
| 3 | Lambda Custom Resource for schema bootstrap | Fully automated, idempotent, re-triggerable via version parameter |
| 4 | CDK `DockerImageAsset` for container images | Simplest path; no separate ECR/CI pipeline needed for Phase 1 |
| 5 | Lambda (60s schedule) for queue-depth metric | Decoupled from worker lifecycle; feeds CloudWatch for auto-scaling |
| 6 | Secrets from env vars at deploy time | Never hardcoded; read from `API_AUTH_PASSWORD`, `TAVILY_API_KEY`, etc. |
| 7 | ALB with IP whitelist + password auth | Security group restricts to user-specified IP; password checked in API service |
| 8 | Optional custom domain + ACM cert | Provided via CDK context when ready; stack works without it |
| 9 | Aurora Serverless v2 min 0.5 / max 4 ACU | Lowest cost; sufficient for MVP scale |

---

## Stack Architecture

Three stacks with explicit cross-stack references:

```
┌─────────────────────────────────────────────────────────────┐
│  PersistentAgentRuntime-{envName}-Network                   │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ VPC (2 AZs)                                         │    │
│  │  ├── Public Subnets (ALB)                           │    │
│  │  ├── Private Subnets w/ NAT (ECS Fargate)           │    │
│  │  └── Isolated Subnets (Aurora)                      │    │
│  │ 1x NAT Gateway                                      │    │
│  │ Security Groups: ALB, API, Worker, DB, Lambda       │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  PersistentAgentRuntime-{envName}-Data                      │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Aurora Serverless v2 (PostgreSQL 16)                 │    │
│  │  ├── Min 0.5 ACU / Max 4 ACU                        │    │
│  │  ├── Isolated subnets only                          │    │
│  │  └── Credentials → Secrets Manager (auto-generated) │    │
│  │ Schema Bootstrap Lambda (Custom Resource)            │    │
│  │ Third-party API Key Secrets (from env vars)          │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  PersistentAgentRuntime-{envName}-Compute                   │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ ECS Cluster (Fargate)                               │    │
│  │  ├── API Service (Spring Boot, port 8080)           │    │
│  │  │    ├── Task Def + Fargate Service                │    │
│  │  │    ├── ALB → Target Group                        │    │
│  │  │    ├── Auto-scale on CPU (target 60%)            │    │
│  │  │    └── DockerImageAsset build                    │    │
│  │  └── Worker Service (Python, no inbound)            │    │
│  │       ├── Task Def + Fargate Service                │    │
│  │       ├── Auto-scale on queue depth metric          │    │
│  │       └── DockerImageAsset build                    │    │
│  │ Queue Depth Lambda (EventBridge 60s schedule)       │    │
│  │ ALB (public subnets, IP-restricted SG)              │    │
│  │ Optional: ACM Certificate + HTTPS listener          │    │
│  │ IAM: Task execution roles (least privilege)         │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

---

## Implementation Steps

### Step 0: CDK Project Bootstrap

**Goal:** Initialize the CDK TypeScript project.

- Run `cdk init app --language typescript` in `infrastructure/cdk/`
- Configure `cdk.json` with context parameters:
  - `envName` (default: `dev`)
  - `allowedIp` — prompted at deploy time, no default (required)
  - `domainName` — optional, enables ACM cert + HTTPS when provided
  - `migrationVersion` — default `1`, bump to force schema re-run
- Add dependencies: `@aws-cdk/aws-lambda-python-alpha` (for Python Lambda bundling), standard CDK constructs
- Set up `bin/app.ts` entry point instantiating the 3 stacks with cross-stack dependencies
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
  - `Public` — ALB (CIDR mask 24)
  - `Private with NAT` — ECS Fargate tasks (CIDR mask 24)
  - `Isolated` — Aurora cluster (CIDR mask 24)
- 1 NAT Gateway (`natGateways: 1`) to minimize cost

**Security Groups:**
- `albSg` — Inbound: TCP 443 (or 80 if no domain) from `allowedIp` CIDR only. Outbound: to API SG on 8080.
- `apiServiceSg` — Inbound: TCP 8080 from ALB SG only. Outbound: to DB SG on 5432, to internet (NAT) for any external calls.
- `workerServiceSg` — Inbound: none. Outbound: to DB SG on 5432, to internet (NAT) for LLM API calls.
- `dbSg` — Inbound: TCP 5432 from API SG, Worker SG, and Lambda SG. Outbound: none.
- `lambdaSg` — Inbound: none. Outbound: to DB SG on 5432.

**Exports:** VPC, all security groups, subnet selections (for use by Data and Compute stacks).

---

### Step 2: Data Stack

**Goal:** Aurora Serverless v2 cluster, Secrets Manager secrets, schema bootstrap.

**Aurora Serverless v2:**
- Engine: PostgreSQL 16 (compatible)
- Writer instance: `ServerlessV2ClusterInstanceProps` with min 0.5 ACU, max 4 ACU
- Subnet group: isolated subnets
- Security group: `dbSg`
- Credentials: auto-generated by CDK, stored in Secrets Manager (`aurora-{envName}-credentials`)
- Database name: `persistent_agent_runtime`
- Deletion protection: off for MVP (configurable)
- Backup retention: 7 days
- No public accessibility

**Third-party Secrets:**
- `ApiAuthPassword` — read from env var `API_AUTH_PASSWORD` at synth time, stored in Secrets Manager
- `TavilyApiKey` — read from env var `TAVILY_API_KEY` at synth time, stored in Secrets Manager
- `AnthropicApiKey` — read from env var `ANTHROPIC_API_KEY` at synth time, stored in Secrets Manager
- All read via `process.env.XXX` with a clear error message if missing

**Schema Bootstrap Lambda (Custom Resource):**
- Runtime: Python 3.12 or Node.js 20
- Code: Reads `infrastructure/database/migrations/0001_phase1_durable_execution.sql`, connects to Aurora via credentials from Secrets Manager, executes SQL
- VPC-attached: private subnets, `lambdaSg`
- Idempotency: The Custom Resource's `physicalResourceId` is set to `schema-v{migrationVersion}`. CloudFormation only re-invokes when this ID changes. Bumping `migrationVersion` in `cdk.json` context forces re-execution.
- The SQL itself is safe to re-run (`CREATE TABLE` will error on duplicate — use `IF NOT EXISTS` wrapper or catch-and-ignore in the Lambda handler)
- IAM: Read access to DB credentials secret, VPC network interfaces

**Exports:** Aurora cluster endpoint, port, DB name, credentials secret ARN, API key secret ARNs.

---

### Step 3: Dockerfiles

**Goal:** Production container images for both services.

**`services/api-service/Dockerfile`:**
```dockerfile
# Stage 1: Build
FROM eclipse-temurin:21-jdk AS build
WORKDIR /app
COPY gradle/ gradle/
COPY gradlew build.gradle settings.gradle ./
RUN ./gradlew dependencies --no-daemon   # cache deps
COPY src/ src/
RUN ./gradlew bootJar --no-daemon

# Stage 2: Runtime
FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /app/build/libs/*.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
```

**`services/api-service/.dockerignore`:**
```
build/
.gradle/
*.md
src/test/
```

**`services/worker-service/Dockerfile`:**
```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir pip --upgrade
COPY pyproject.toml ./
RUN pip install --no-cache-dir .   # install deps from pyproject.toml
COPY . .
ENTRYPOINT ["python", "-m", "core.worker"]
```
> Note: The exact entrypoint will be confirmed by reading the worker service code. It may be `core.worker` or a `__main__.py` — adjust accordingly.

**`services/worker-service/.dockerignore`:**
```
__pycache__/
*.pyc
.pytest_cache/
tests/
*.md
.env
```

---

### Step 4: API Service Password Auth Filter

**Goal:** Add an authentication filter to the Spring Boot API service that validates a password from the `X-Auth-Password` request header.

**Implementation:**
- Create `services/api-service/src/main/java/com/persistentagent/api/config/AuthFilter.java`
  - A `OncePerRequestFilter` (Spring Security is NOT needed — simple servlet filter)
  - Reads expected password from env var `AUTH_PASSWORD` (injected from Secrets Manager by ECS)
  - Compares `request.getHeader("X-Auth-Password")` against the expected password using constant-time comparison
  - Exempts `GET /v1/health` (so ALB health checks pass without auth)
  - Returns `401 Unauthorized` with JSON body on mismatch
- Register the filter via a `@Component` annotation or a `FilterRegistrationBean` in a config class
- Update `application.yml` to add `auth.password: ${AUTH_PASSWORD:}` (empty default for local dev)

**Testing:**
- Unit test: Verify filter blocks requests without valid password and allows valid ones
- Verify health endpoint bypasses auth

---

### Step 5: Compute Stack — ECS Cluster & ALB

**Goal:** ECS Fargate cluster, ALB, and service infrastructure.

**ECS Cluster:**
- Cluster name: `par-{envName}`
- Container Insights enabled (CloudWatch)

**Application Load Balancer:**
- Scheme: internet-facing
- Subnets: public
- Security group: `albSg` (IP-restricted)
- Listeners:
  - If `domainName` provided: HTTPS (443) with ACM certificate, redirect HTTP→HTTPS
  - If no `domainName`: HTTP (80) only
- Target group: API Service on port 8080, health check `GET /v1/health` (200 OK, no auth header needed)

**ACM Certificate (conditional):**
- Created only when `domainName` context parameter is provided
- DNS validation — user adds CNAME at their DNS provider
- Attached to HTTPS listener

---

### Step 6: Compute Stack — API Service (ECS Fargate)

**Goal:** Fargate service for the Spring Boot API.

**Task Definition:**
- CPU: 512 (0.5 vCPU)
- Memory: 1024 MB
- Container image: `DockerImageAsset` from `services/api-service/`
- Port mapping: 8080
- Log driver: `awslogs` (CloudWatch log group `/ecs/par-{envName}/api-service`)
- Environment variables:
  - `DB_HOST` — Aurora cluster endpoint
  - `DB_PORT` — Aurora port
  - `DB_NAME` — `persistent_agent_runtime`
  - `SERVER_PORT` — `8080`
- Secrets (from Secrets Manager):
  - `DB_USER` — from Aurora credentials secret (username field)
  - `DB_PASSWORD` — from Aurora credentials secret (password field)
  - `AUTH_PASSWORD` — from API auth password secret

**Fargate Service:**
- Desired count: 1
- Subnets: private (with NAT)
- Security group: `apiServiceSg`
- Attach to ALB target group
- Health check grace period: 120s (Spring Boot startup)

**Auto-scaling:**
- Min: 1, Max: 4
- Target tracking: CPU utilization at 60%

**IAM Task Execution Role:**
- `AmazonECSTaskExecutionRolePolicy` (managed)
- Read access to Secrets Manager secrets (DB creds, auth password)
- CloudWatch Logs: `CreateLogGroup`, `CreateLogStream`, `PutLogEvents`

**IAM Task Role:**
- CloudWatch Logs write
- (No Bedrock/S3 needed for API service)

---

### Step 7: Compute Stack — Worker Service (ECS Fargate)

**Goal:** Fargate service for the Python worker.

**Task Definition:**
- CPU: 1024 (1 vCPU)
- Memory: 2048 MB (LangGraph + LLM response buffering)
- Container image: `DockerImageAsset` from `services/worker-service/`
- No port mapping (no inbound traffic)
- Log driver: `awslogs` (CloudWatch log group `/ecs/par-{envName}/worker-service`)
- Environment variables:
  - `DB_HOST` — Aurora cluster endpoint
  - `DB_PORT` — Aurora port
  - `DB_NAME` — `persistent_agent_runtime`
  - `AWS_REGION` — stack region (for Bedrock if used)
- Secrets (from Secrets Manager):
  - `DB_USER` — from Aurora credentials secret
  - `DB_PASSWORD` — from Aurora credentials secret
  - `TAVILY_API_KEY` — from Tavily secret
  - `ANTHROPIC_API_KEY` — from Anthropic secret

**Fargate Service:**
- Desired count: 1
- Subnets: private (with NAT)
- Security group: `workerServiceSg`
- No load balancer (pulls tasks from DB)

**Auto-scaling:**
- Min: 1, Max: 8
- Target tracking on custom CloudWatch metric `QueueDepth` (see Step 8)
- Scale-out: when `QueueDepth > 5` per worker instance
- Cooldown: 60s scale-out, 300s scale-in

**IAM Task Execution Role:**
- Same as API service execution role pattern
- Read access to all relevant secrets

**IAM Task Role:**
- CloudWatch Logs write
- Bedrock `InvokeModel` (for `anthropic.claude-*` models, if using Bedrock)
- CloudWatch `PutMetricData` (for any app-level metrics)

---

### Step 8: Queue Depth Lambda (Scheduled)

**Goal:** Publish `QueueDepth` custom metric to CloudWatch every 60 seconds.

**Lambda:**
- Runtime: Python 3.12
- Code location: `infrastructure/cdk/lambda/queue-depth/`
- Handler logic:
  1. Read Aurora credentials from Secrets Manager
  2. Connect to Aurora PostgreSQL
  3. Execute `SELECT COUNT(*) FROM tasks WHERE status = 'queued'`
  4. Publish to CloudWatch: namespace `PersistentAgentRuntime`, metric `QueueDepth`, dimension `Environment={envName}`
- VPC-attached: private subnets, `lambdaSg`
- Timeout: 30s
- Memory: 256 MB
- Dependencies: `psycopg2-binary` (bundled via Lambda layer or Docker-based build)

**EventBridge Rule:**
- Schedule: `rate(1 minute)`
- Target: Queue Depth Lambda

**IAM:**
- Read access to Aurora credentials secret
- `cloudwatch:PutMetricData`
- VPC network interface permissions

---

### Step 9: CDK Unit Tests

**Goal:** Validate synthesized templates using CDK `Assertions`.

**Test file:** `infrastructure/cdk/test/stacks.test.ts`

**Assertions to verify:**
- Network stack:
  - VPC has 2 AZs
  - Exactly 1 NAT Gateway
  - 5 security groups exist
- Data stack:
  - Aurora cluster exists with PostgreSQL engine
  - Cluster is NOT publicly accessible
  - Credentials secret exists in Secrets Manager
  - Custom Resource for schema bootstrap exists
- Compute stack:
  - ECS cluster exists
  - 2 Fargate services (API + Worker)
  - ALB exists in public subnets
  - API task def has correct environment variables and secrets
  - Worker task def has no port mappings
  - Auto-scaling policies exist for both services
  - All resources tagged with `Project: PersistentAgentRuntime`

---

### Step 10: Infrastructure README

**Goal:** `infrastructure/README.md` documenting deploy/destroy workflow.

**Contents:**
1. Prerequisites (AWS CLI, CDK CLI, Docker, Node.js)
2. AWS credentials setup (`~/.aws/credentials` — never committed)
3. Required environment variables for deploy:
   - `API_AUTH_PASSWORD`
   - `TAVILY_API_KEY`
   - `ANTHROPIC_API_KEY`
4. CDK context parameters:
   - `allowedIp` (required)
   - `domainName` (optional)
   - `migrationVersion` (default 1)
   - `envName` (default dev)
5. Commands:
   - `cdk synth` — generate templates
   - `cdk deploy --all` — deploy all stacks
   - `cdk deploy --all -c allowedIp=76.13.123.191/32` — deploy with IP
   - `cdk destroy --all` — tear down
6. Post-deploy: DNS CNAME for custom domain (if applicable)
7. Re-running schema migration: bump `migrationVersion`
8. Cost estimate (Aurora min ACU + 1 NAT + 2 Fargate tasks)

---

## Deploy-Time Input Summary

| Input | Source | Required | Purpose |
|-------|--------|----------|---------|
| `API_AUTH_PASSWORD` | Env var | Yes | API service authentication |
| `TAVILY_API_KEY` | Env var | Yes | Worker web_search tool |
| `ANTHROPIC_API_KEY` | Env var | Yes | Worker LLM calls |
| `allowedIp` | CDK context (`-c`) | Yes | ALB security group IP whitelist |
| `domainName` | CDK context (`-c`) | No | ACM cert + HTTPS listener |
| `envName` | CDK context (`-c`) | No (default: `dev`) | Stack name prefix |
| `migrationVersion` | CDK context (`-c`) | No (default: `1`) | Force schema re-run |
| AWS credentials | `~/.aws/credentials` | Yes | CDK deploy target account |

---

## Execution Order

```
Step 0: CDK project init
Step 3: Dockerfiles (can parallel with Steps 1-2)
Step 4: API auth filter (can parallel with Steps 1-2)
   ↓
Step 1: Network Stack
   ↓
Step 2: Data Stack (depends on Network)
   ↓
Steps 5-8: Compute Stack (depends on Network + Data)
   ↓
Step 9: CDK unit tests
Step 10: README
```

Steps 0, 3, 4 have no CDK dependencies and can be done first or in parallel.

---

## Cost Estimate (MVP / dev)

| Resource | Monthly Cost (approx) |
|----------|-----------------------|
| Aurora Serverless v2 (0.5 ACU min, mostly idle) | ~$22 |
| NAT Gateway (1x) + data processing | ~$35 |
| ECS Fargate — API (0.5 vCPU, 1GB, 1 task) | ~$15 |
| ECS Fargate — Worker (1 vCPU, 2GB, 1 task) | ~$30 |
| ALB | ~$16 |
| CloudWatch Logs | ~$5 |
| Lambda (schema + queue depth) | <$1 |
| Secrets Manager (4 secrets) | ~$2 |
| **Total** | **~$126/month** |

---

## Future Multi-Stage Expansion

When ready to add staging/prod:
1. Pass `envName=staging` or `envName=prod` as CDK context
2. This creates entirely new stacks (`PersistentAgentRuntime-staging-Network`, etc.)
3. No existing `dev` resources are touched
4. Adjust ACU limits, NAT gateway count, and auto-scaling ranges per environment
5. Move to CDK Pipelines for CI/CD in a future phase
