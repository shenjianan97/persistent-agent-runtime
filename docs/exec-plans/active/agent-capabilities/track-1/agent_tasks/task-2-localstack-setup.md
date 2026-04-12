<!-- AGENT_TASK_START: task-2-localstack-setup.md -->

# Task 2 — LocalStack Docker Setup for S3 Emulation

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 2: Artifact Storage, Local Development subsection)
2. `docs/exec-plans/active/agent-capabilities/track-1/plan.md` — Track 1 execution plan
3. `Makefile` — current `db-up`, `db-down`, `start`, `stop` targets and database container management

**CRITICAL POST-WORK:** After completing this task:
1. Run `make db-up` and verify that both PostgreSQL and LocalStack containers start correctly.
2. Verify the `platform-artifacts` S3 bucket exists by running: `aws --endpoint-url=http://localhost:4566 s3 ls` (or the init script).
3. Run `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
4. Update the status in `docs/exec-plans/active/agent-capabilities/track-1/progress.md` to "Done".

## Context

Track 1 needs S3 storage for artifact files. For local development, we use LocalStack — a local AWS emulator running in Docker. The worker uses `boto3` and the API uses AWS SDK v2, both pointed at `http://localhost:4566`. When deploying to real AWS, only the endpoint changes — no code path difference.

This task sets up:
- A `docker-compose.yml` with LocalStack (S3 emulation) and the existing PostgreSQL container
- An init script that creates the `platform-artifacts` S3 bucket on LocalStack startup
- Makefile targets for managing LocalStack alongside the database

## Task-Specific Shared Contract

- S3 bucket name: `platform-artifacts`
- LocalStack endpoint: `http://localhost:4566`
- LocalStack image: `localstack/localstack:3`
- Dummy AWS credentials: `AWS_ACCESS_KEY_ID=test`, `AWS_SECRET_ACCESS_KEY=test`
- AWS region: `us-east-1`
- The `docker-compose.yml` must also include the PostgreSQL container so that `make db-up` can use compose instead of raw `docker run`
- Environment variable `S3_ENDPOINT_URL` controls the S3 endpoint. When unset, boto3/AWS SDK uses real AWS.

## Affected Component

- **Service/Module:** Infrastructure — Docker and Makefile
- **File paths:**
  - `docker-compose.yml` (new)
  - `scripts/init-localstack.sh` (new)
  - `Makefile` (modify — update `db-up`, `db-down`, `db-status` to use docker-compose; add LocalStack targets)
- **Change type:** new files + modification

## Dependencies

- **Must complete first:** None (entry point task, can run in parallel with Task 1)
- **Provides output to:** Task 3 (Worker S3 Client), Task 4 (API S3 Service), Task 6 (upload_artifact Tool), Task 8 (Integration Tests)
- **Shared interfaces/contracts:** LocalStack S3 endpoint URL, bucket name, AWS credentials

## Implementation Specification

### Step 1: Create docker-compose.yml

Create `docker-compose.yml` in the project root:

```yaml
version: "3.8"

services:
  postgres:
    image: postgres:16
    container_name: persistent-agent-runtime-postgres
    environment:
      POSTGRES_USER: ${DB_USER:-postgres}
      POSTGRES_PASSWORD: ${DB_PASSWORD:-postgres}
      POSTGRES_DB: ${DB_NAME:-persistent_agent_runtime}
    ports:
      - "${DB_PORT:-55432}:5432"
    command: postgres -c log_statement=all
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER:-postgres} -d ${DB_NAME:-persistent_agent_runtime}"]
      interval: 2s
      timeout: 5s
      retries: 15

  localstack:
    image: localstack/localstack:3
    container_name: persistent-agent-runtime-localstack
    environment:
      SERVICES: s3
      AWS_DEFAULT_REGION: us-east-1
      AWS_ACCESS_KEY_ID: test
      AWS_SECRET_ACCESS_KEY: test
    ports:
      - "4566:4566"
    volumes:
      - "./scripts/init-localstack.sh:/etc/localstack/init/ready.d/init-localstack.sh"
      - localstackdata:/var/lib/localstack
    healthcheck:
      test: ["CMD-SHELL", "awslocal s3 ls"]
      interval: 5s
      timeout: 10s
      retries: 10

volumes:
  pgdata:
  localstackdata:
```

### Step 2: Create init-localstack.sh

Create `scripts/init-localstack.sh`:

```bash
#!/bin/bash
# Initialize LocalStack S3 bucket for artifact storage.
# This script runs automatically when LocalStack reaches the "ready" state
# via the /etc/localstack/init/ready.d/ mount.

set -euo pipefail

echo "Creating platform-artifacts S3 bucket..."
awslocal s3 mb s3://platform-artifacts 2>/dev/null || true
echo "LocalStack S3 initialization complete."
awslocal s3 ls
```

Make the script executable:
```bash
chmod +x scripts/init-localstack.sh
```

### Step 3: Update Makefile — replace db-up target

Replace the existing `db-up` target to use `docker compose` for both PostgreSQL and LocalStack. The new target must preserve the existing behavior (wait for PostgreSQL readiness) and add LocalStack startup.

Update the `db-up` target:

```makefile
COMPOSE_FILE := $(ROOT_DIR)/docker-compose.yml
LOCALSTACK_CONTAINER_NAME ?= persistent-agent-runtime-localstack

db-up:
	@echo "$(YELLOW)🐳 Ensuring Database and LocalStack containers are running...$(NC)"
	@docker info >/dev/null 2>&1 || (echo "$(RED)❌ Docker daemon is not running. Please start Docker Desktop.$(NC)" && exit 1)
	@if [ "$(DB_HOST)" != "localhost" ] && [ "$(DB_HOST)" != "127.0.0.1" ]; then \
		echo "$(RED)❌ db-up only manages Docker instances on localhost. Current DB_DSN host is '$(DB_HOST)'. Start that database manually instead.$(NC)"; \
		exit 1; \
	fi
	@DB_USER="$(DB_USER)" DB_PASSWORD="$(DB_PASSWORD)" DB_NAME="$(DB_NAME)" DB_PORT="$(DB_PORT)" \
		docker compose -f $(COMPOSE_FILE) up -d postgres localstack
	@echo "$(YELLOW)⏳ Waiting for PostgreSQL to accept connections...$(NC)"
	@attempts=0; \
	until docker exec $(DB_CONTAINER_NAME) env PGPASSWORD="$(DB_PASSWORD)" pg_isready -h 127.0.0.1 -p 5432 -U "$(DB_USER)" -d "$(DB_NAME)" >/dev/null 2>&1; do \
		attempts=$$((attempts + 1)); \
		if [ $$attempts -ge 30 ]; then \
			echo "$(RED)❌ PostgreSQL did not become ready within 30 seconds$(NC)"; \
			exit 1; \
		fi; \
		sleep 1; \
	done
	@echo "$(YELLOW)⏳ Waiting for LocalStack to be ready...$(NC)"
	@attempts=0; \
	until docker exec $(LOCALSTACK_CONTAINER_NAME) awslocal s3 ls >/dev/null 2>&1; do \
		attempts=$$((attempts + 1)); \
		if [ $$attempts -ge 30 ]; then \
			echo "$(RED)❌ LocalStack did not become ready within 30 seconds$(NC)"; \
			exit 1; \
		fi; \
		sleep 1; \
	done
	@echo "$(GREEN)✅ DB and LocalStack are up$(NC)"
```

### Step 4: Update Makefile — replace db-down and db-status targets

```makefile
db-down:
	@echo "$(YELLOW)🛑 Stopping Database and LocalStack containers...$(NC)"
	@docker compose -f $(COMPOSE_FILE) down
	@echo "$(GREEN)✅ Containers stopped$(NC)"

db-status:
	@docker compose -f $(COMPOSE_FILE) ps
```

### Step 5: Add S3 environment variables to .env.localdev (if it exists) or document them

Add these default environment variables to the Makefile configuration section (alongside existing DB vars):

```makefile
S3_ENDPOINT_URL ?= http://localhost:4566
S3_BUCKET_NAME ?= platform-artifacts
AWS_ACCESS_KEY_ID ?= test
AWS_SECRET_ACCESS_KEY ?= test
AWS_REGION ?= us-east-1
```

## Acceptance Criteria

- [ ] `docker-compose.yml` exists in project root with `postgres` and `localstack` services
- [ ] PostgreSQL service configuration matches existing `db-up` behavior (port 55432, same env vars)
- [ ] LocalStack service uses `localstack/localstack:3` image with S3 service enabled
- [ ] `scripts/init-localstack.sh` exists and is executable
- [ ] Init script creates `platform-artifacts` S3 bucket on LocalStack startup
- [ ] `make db-up` starts both PostgreSQL and LocalStack containers
- [ ] `make db-down` stops both containers
- [ ] `make db-status` shows status of both containers
- [ ] PostgreSQL is accessible on port 55432 after `make db-up`
- [ ] LocalStack S3 is accessible on port 4566 after `make db-up`
- [ ] `aws --endpoint-url=http://localhost:4566 s3 ls` shows `platform-artifacts` bucket
- [ ] Existing `make db-migrate` still works correctly
- [ ] `make test` passes (no regressions)

## Testing Requirements

- **Manual verification:** Run `make db-up`, then verify PostgreSQL connectivity via `psql` and S3 bucket existence via `awslocal s3 ls`. Run `make db-down` and verify both containers stop. Run `make db-up` again to verify idempotency.
- **Regression:** Run `make db-migrate` after `make db-up` — all migrations 0001-0009 must apply cleanly. Run `make test` to verify no existing tests break.

## Constraints and Guardrails

- Do not change the PostgreSQL container name (`persistent-agent-runtime-postgres`) — other scripts and tests reference it.
- Do not change the PostgreSQL port mapping (55432) — the existing `DB_DSN` default depends on it.
- Do not add application code — this task is infrastructure-only.
- The `init-localstack.sh` must be idempotent (re-running it should not fail if the bucket already exists).
- LocalStack must only run the S3 service (via `SERVICES: s3`) to minimize resource usage.

## Assumptions

- Docker and Docker Compose (v2, the `docker compose` subcommand) are available on the development machine.
- The existing `db-up` target uses raw `docker run`; this task migrates to `docker compose`.
- No other services in the project currently use `docker-compose.yml` (this is a new file).
- The E2E test infrastructure uses a separate PostgreSQL container (`par-e2e-postgres` on port 55433) and is not affected by this change.

<!-- AGENT_TASK_END: task-2-localstack-setup.md -->
