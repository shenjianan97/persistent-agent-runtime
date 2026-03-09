# API Service

REST API service for the Persistent Agent Runtime. Acts as the ingest and query interface between external clients and the PostgreSQL-backed task execution system.

## Endpoints

### POST /v1/tasks

Submit a new task for execution.

**Request:**
```json
{
  "agent_id": "support_agent_v1",
  "agent_config": {
    "system_prompt": "You are a research assistant...",
    "model": "claude-sonnet-4-6",
    "temperature": 0.7,
    "allowed_tools": ["web_search", "read_url", "calculator"]
  },
  "input": "Research topic X",
  "max_retries": 3,
  "max_steps": 15,
  "task_timeout_seconds": 3600
}
```

**Response (201 Created):**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "agent_id": "support_agent_v1",
  "status": "queued",
  "created_at": "2026-03-05T10:00:00Z"
}
```

### GET /v1/tasks

List tasks with optional filters. Supports `status`, `agent_id`, and `limit` query parameters.

**Query Parameters:**
- `status` (optional) — Filter by task status: `queued`, `running`, `completed`, `dead_letter`
- `agent_id` (optional) — Filter by agent ID
- `limit` (optional) — Max results (1-200, default 50)

**Response (200 OK):**
```json
{
  "items": [
    {
      "task_id": "...",
      "agent_id": "support_agent_v1",
      "status": "running",
      "retry_count": 0,
      "checkpoint_count": 3,
      "total_cost_microdollars": 8500,
      "created_at": "2026-03-05T10:00:00Z",
      "updated_at": "2026-03-05T10:00:15Z"
    }
  ],
  "total": 1
}
```

### GET /v1/tasks/{task_id}

Get task status with checkpoint aggregates.

**Response (200 OK):**
```json
{
  "task_id": "...",
  "agent_id": "support_agent_v1",
  "status": "running",
  "input": "Research topic X",
  "output": null,
  "retry_count": 0,
  "retry_history": [],
  "checkpoint_count": 5,
  "total_cost_microdollars": 12500,
  "lease_owner": "worker-abc-123",
  "last_error_code": null,
  "last_error_message": null,
  "last_worker_id": null,
  "dead_letter_reason": null,
  "dead_lettered_at": null,
  "created_at": "2026-03-05T10:00:00Z",
  "updated_at": "2026-03-05T10:00:15Z"
}
```

### GET /v1/tasks/{task_id}/checkpoints

Get checkpoint history for a task. Returns root-namespace checkpoints ordered by creation time.

**Response (200 OK):**
```json
{
  "checkpoints": [
    {
      "checkpoint_id": "...",
      "step_number": 1,
      "node_name": "agent",
      "worker_id": "worker-a-123",
      "cost_microdollars": 5200,
      "execution_metadata": { "latency_ms": 2340 },
      "created_at": "2026-03-05T10:00:01Z"
    }
  ]
}
```

### POST /v1/tasks/{task_id}/cancel

Cancel a queued or running task. Moves it to dead_letter with reason `cancelled_by_user`.

**Response (200 OK):**
```json
{
  "task_id": "...",
  "status": "dead_letter",
  "dead_letter_reason": "cancelled_by_user"
}
```

### GET /v1/tasks/dead-letter

List dead-lettered tasks. Supports optional `agent_id` filter and `limit` parameter (default 50, max 200).

**Query Parameters:**
- `agent_id` (optional) - Filter by agent ID
- `limit` (optional) - Max results to return (1-200, default 50)

**Response (200 OK):**
```json
{
  "items": [
    {
      "task_id": "...",
      "agent_id": "support_agent_v1",
      "dead_letter_reason": "non_retryable_error",
      "last_error_code": "tool_args_invalid",
      "last_error_message": "validation failed",
      "retry_count": 1,
      "last_worker_id": "worker-a-123",
      "dead_lettered_at": "2026-03-05T10:00:20Z"
    }
  ]
}
```

### POST /v1/tasks/{task_id}/redrive

Redrive a dead-lettered task back to queued state. Resets retry_count and clears error fields.

**Response (200 OK):**
```json
{
  "task_id": "...",
  "status": "queued"
}
```

### GET /v1/health

Health check with database connectivity and queue/worker counts. `active_workers` counts workers registered in the `workers` table with a heartbeat within the last 60 seconds (includes idle workers, not just those running tasks).

**Response (200 OK):**
```json
{
  "status": "healthy",
  "database": "connected",
  "active_workers": 3,
  "queued_tasks": 12
}
```

## Validation Rules

| Field | Constraint |
|-------|-----------|
| `agent_id` | Required, max 64 characters |
| `input` | Required, max 100KB |
| `agent_config.system_prompt` | Required, max 50KB |
| `agent_config.model` | Required, must be a supported model |
| `agent_config.temperature` | 0.0 - 2.0 (default 0.7) |
| `agent_config.allowed_tools` | Each tool must be in: `web_search`, `read_url`, `calculator` |
| `max_retries` | 0 - 10 (default 3) |
| `max_steps` | 1 - 1000 (default 100) |
| `task_timeout_seconds` | 60 - 86400 (default 3600) |

**Supported Models:**
`claude-sonnet-4-6`, `claude-sonnet-4-20250514`, `claude-haiku-4-20250514`, `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`, `us.anthropic.claude-sonnet-4-20250514-v1:0`, `us.anthropic.claude-haiku-4-20250514-v1:0`

## Build and Run

**Prerequisites:**
- Java 21+
- PostgreSQL with the Phase 1 schema applied (see `infrastructure/database/`)

**Build:**
```bash
cd services/api-service

# Note: If you encounter an error like "Unable to access jarfile gradle-wrapper.jar",
# you may need to regenerate the wrapper using a local gradle installation first:
# gradle wrapper

./gradlew build
```

**Run:**
```bash
./gradlew bootRun
```

The service starts on port 8080 by default.

## Configuration

Configuration via environment variables or `application.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_HOST` | `localhost` | PostgreSQL host |
| `DB_PORT` | `55432` | PostgreSQL port |
| `DB_NAME` | `persistent_agent_runtime` | Database name |
| `DB_USER` | `postgres` | Database username |
| `DB_PASSWORD` | `postgres` | Database password |
| `SERVER_PORT` | `8080` | HTTP server port |

## Running Tests

**Unit tests only:**
```bash
./gradlew test
```

**Java Integration tests** (requires local PostgreSQL container):
```bash
INTEGRATION_TESTS_ENABLED=true ./gradlew test
```

The Java integration tests expect the `persistent-agent-runtime-postgres` Docker container running on `localhost:55432`.

**Python API Integration End-to-End Tests:**
A python script `api_integration_test.py` is included to run end-to-end failure scenarios against the running API application and a real database.

```bash
# Ensure API service is running:
./gradlew bootRun

# Then, run the tests:
pip install urllib3 psycopg2-binary
python api_integration_test.py
```
