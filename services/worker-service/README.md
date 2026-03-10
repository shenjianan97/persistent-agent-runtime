# Worker Service

The worker service claims tasks from PostgreSQL using `FOR UPDATE SKIP LOCKED`, executes them via LangGraph with lease-based ownership, and invokes Phase 1 tools as in-process Python functions.

## Architecture

```
WorkerService
├── TaskPoller        LISTEN/NOTIFY + claim loop with exponential backoff
├── HeartbeatManager  Per-task lease extension (15s interval, 60s lease)
├── WorkerRegistry    Self-registration + periodic heartbeat to `workers` table
├── ReaperTask        Reclaims expired leases, dead-letters timed-out tasks, marks stale workers offline
├── TaskRouter        Routes claimed tasks to the appropriate executor
└── GraphExecutor     LangGraph orchestration + in-process tool execution
```

## Quick Start

For normal local development from the repo root, prefer:

```bash
make install
make dev
```

That root workflow installs console and worker dependencies first, then starts the console, API, and worker in one terminal and ensures the local PostgreSQL container is running.

### Prerequisites

- Python 3.11+
- PostgreSQL (for worker execution; not needed for unit tests)

### Install

```bash
cd services/worker-service
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Environment Variables

Create a `.env` file in `services/worker-service/` or export directly:

| Variable | Required For | Default |
|----------|-------------|---------|
| `DB_DSN` | Worker | **Required** (no default) |
| `ANTHROPIC_API_KEY` | Claude models | — |
| `AWS_ACCESS_KEY_ID` | Bedrock models | — |
| `AWS_SECRET_ACCESS_KEY` | Bedrock models | — |
| `AWS_REGION` | Bedrock models | — |
| `TAVILY_API_KEY` | `web_search` tool | — |
| `MODEL_PRICING_FILE` | Custom model pricing map | `config/model_pricing.json` |

The model is selected per-agent via `agent_config.model`. Names containing `claude` use Anthropic; others use Bedrock.
Checkpoint cost estimation uses the local pricing file, not a runtime provider lookup. Update `config/model_pricing.json` or point `MODEL_PRICING_FILE` at another JSON file to add or override model rates.

### Run the Worker (Real LLM)

Requires a running PostgreSQL instance with the schema applied and a valid LLM API key.

```bash
cd services/worker-service
source .venv/bin/activate
export DB_DSN="postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime"
export ANTHROPIC_API_KEY="sk-ant-..."
export TAVILY_API_KEY="tvly-..."

python main.py
```

The worker registers itself in the `workers` table on startup, sends heartbeats every 15s, and deregisters on graceful shutdown (SIGTERM/SIGINT). If a worker crashes without deregistering, the reaper on other workers marks it offline after 90s of missed heartbeats.

### Run the Worker with Mock LLM (Development)

There is no built-in mock LLM mode in the worker process. For local development without LLM credentials:

1. **Unit tests** — run the full pytest suite (no DB or API keys needed):
   ```bash
   pytest tests/ -v
   ```
   Tests mock `ChatAnthropic` via `unittest.mock.patch("executor.graph.ChatAnthropic")` and inject fake `AIMessage` responses.

2. **Backend integration tests** — cross-service tests (API + Worker + PostgreSQL) located in [`tests/backend-integration/`](../../tests/backend-integration/). Workers are wired up directly via test helpers, not through `main.py`.

## Tools

The `GraphExecutor` calls tool functions directly as in-process LangGraph `StructuredTool` instances — no MCP protocol or HTTP involved at runtime. Three read-only tools are available:

| Tool | Description | Credentials |
|------|-------------|-------------|
| `web_search(query, max_results=5)` | Tavily-backed web search | `TAVILY_API_KEY` |
| `read_url(url, max_chars=5000)` | Bounded URL fetch with SSRF guards | None |
| `calculator(expression)` | Safe AST-based arithmetic | None |

The `tools/` package can also run as a standalone FastMCP server for manual testing and future independent deployment. See [`tools/README.md`](tools/README.md) for how to run the standalone server, the tool contract, and test coverage.

## Running Tests

```bash
cd services/worker-service
pytest tests/ -v
```

No PostgreSQL or API keys required. Tests use mocked DB connections and fake tool providers.

## Module Structure

```
main.py               Entry point: asyncio main loop
core/
  config.py           WorkerConfig (frozen dataclass, all tunable parameters)
  db.py               asyncpg pool + LISTEN connection factory
  poller.py           Task claim loop
  heartbeat.py        Per-task lease extension + revocation detection
  reaper.py           Expired lease scanner
  worker.py           Top-level orchestrator + signal handling
  logging.py          Structured logging (structlog JSON) + MetricsCollector
executor/
  router.py           TaskRouter protocol + DefaultTaskRouter
  graph.py            LangGraph assembly, LLM binding, error classification
checkpointer/
  postgres.py         Lease-aware LangGraph checkpoint saver
tools/
  app.py              FastMCP application assembly
  server.py           CLI entrypoint (stdio/HTTP)
  definitions.py      Tool schemas, models, registration
  calculator.py       Safe arithmetic evaluator
  read_url.py         URL fetch + HTML extraction
  providers/search.py SearchProvider protocol + Tavily implementation
  env.py              .env loading
  sample_client.py    Manual HTTP test client
```
