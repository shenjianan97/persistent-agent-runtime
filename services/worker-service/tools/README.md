# Tools Package

This package contains the Phase 1 tool implementations and a standalone FastMCP server. It exposes three read-only built-in tools:

- `web_search`
- `read_url`
- `calculator`

**At runtime, the `GraphExecutor` does not use the MCP server.** It imports the tool functions directly from this package and wraps them as LangGraph `StructuredTool` instances for in-process execution. The standalone MCP server (`server.py`) exists for manual testing and future independent deployment.

## Files

- `app.py` builds the `FastMCP` application with no worker-specific runtime concerns.
- `server.py` is the current worker-owned runtime shim and stdio/HTTP entrypoint.
- `definitions.py` is the canonical source of tool names, descriptions, input schemas, output schemas, and registration.
- `env.py` loads local `.env` files for tool configuration.
- `runtime_logging.py` configures stderr logging for server startup and tool calls.
- `sample_client.py` is a tiny manual HTTP client for local testing.
- `calculator.py` implements safe arithmetic evaluation.
- `read_url.py` implements bounded URL fetching, SSRF-style host rejection, and text extraction.
- `providers/search.py` contains the search provider abstraction and the Tavily-backed default implementation.

## Running The Standalone MCP Server

Run over stdio:

```bash
cd services/worker-service
.venv/bin/python -m tools.server
```

Run over HTTP:

```bash
cd services/worker-service
.venv/bin/python -m tools.server --transport http --host 127.0.0.1 --port 8000
```

Connect with the sample client:

```bash
cd services/worker-service
.venv/bin/python -m tools.sample_client --url http://127.0.0.1:8000/mcp
```

## Tool Contract

`list_tools()` advertises exactly these tool schemas:

- `web_search(query: str, max_results: int = 5)`
  - `query` is required
  - `max_results` is bounded to `1..10`
- `read_url(url: str, max_chars: int = 5000)`
  - `url` must be public `http` or `https`
  - `max_chars` is bounded to `500..20000`
- `calculator(expression: str)`
  - `expression` is bounded and only arithmetic syntax is allowed

Structured outputs are returned for all three tools so the executor can consume them without ad hoc parsing.

## Configuration

`web_search` uses DuckDuckGo and requires no API key or credentials.

The tools package will load `.env` files automatically for local development:

- current working directory `.env`
- `services/worker-service/tools/.env`
- `services/worker-service/.env`

Shell environment variables still win because `.env` values do not override an already-set variable.

`read_url` and `calculator` also do not require external credentials.

## How To Test

Run the full worker-service test suite:

```bash
cd .
services/worker-service/.venv/bin/python -m pytest services/worker-service/tests -q
```

Run only the MCP server tests:

```bash
cd .
services/worker-service/.venv/bin/python -m pytest \
  services/worker-service/tests/test_env_loading.py \
  services/worker-service/tests/test_mcp_server.py \
  services/worker-service/tests/test_mcp_http_integration.py \
  services/worker-service/tests/test_mcp_stdio_integration.py -q
```

## Test Coverage

- `test_mcp_server.py` instantiates the real `FastMCP` server in-process and verifies `list_tools()` plus direct tool invocation.
- `test_mcp_http_integration.py` launches the server over local streamable HTTP and connects with a real MCP client session.
- `test_mcp_stdio_integration.py` launches the server as a subprocess over stdio and connects to it with a real MCP `ClientSession`.
- `test_env_loading.py` verifies `.env` loading precedence for current working directory, `tools/.env`, and existing process environment variables.
- `test_web_search_tool.py` verifies result normalization and error propagation with fake search providers.
- `test_read_url_tool.py` verifies URL validation, private-address rejection, content filtering, extraction, and truncation.
- `test_calculator_tool.py` verifies allowed arithmetic and rejection of unsafe syntax.

## Logging

The tools package logs runtime events to `stderr`, not `stdout`.

- startup logs for stdio and HTTP server entrypoints go to `stderr`
- tool call start/success/failure logs go to `stderr`
- in stdio mode this is important because `stdout` is reserved for MCP protocol traffic

When running the HTTP server locally, you should expect both FastMCP/Uvicorn logs and the tool runtime logs on `stderr`.

## Notes

- The stdio subprocess test uses deterministic fake dependencies for `web_search` and mocked HTTP transport for `read_url`. It tests the MCP transport and server wiring without depending on public internet access.
- `app.py` is intentionally the portable assembly layer so this package can move into a future standalone `services/mcp-server/` module with minimal changes.
