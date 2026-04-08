<!-- AGENT_TASK_START: task-8-integration-tests.md -->

# Task 8 — Integration Tests: Custom Tool Lifecycle E2E

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` — canonical design contract (Testing Strategy section)
2. `services/worker-service/tests/test_mcp_http_integration.py` — existing MCP HTTP integration test pattern
3. `services/worker-service/tests/test_mcp_server.py` — existing MCP server test pattern
4. `services/worker-service/tests/fixtures/http_test_server.py` — existing test server fixture
5. `tests/backend-integration/` — existing backend integration test structure (if present)
6. `services/worker-service/executor/mcp_session.py` — Task 4 output: `McpSessionManager`
7. `services/worker-service/executor/schema_converter.py` — Task 5 output: schema converter
8. `services/worker-service/executor/graph.py` — Task 5 modifications: `_lookup_tool_server_configs()`, custom tool integration

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all tests pass (including new integration tests). Fix any failures.
2. Run `make e2e-test` if available and verify.
3. Update the status in `docs/exec-plans/active/phase-2/track-4/progress.md` to "Done".

## Context

Track 4 introduces the full custom tool lifecycle: operators register MCP tool servers via the API, agents reference them in config, and the worker discovers and invokes custom tools during task execution. Integration tests must verify this end-to-end flow, including error paths.

The existing test infrastructure includes MCP test server fixtures (`http_test_server.py`) that can be reused for Track 4 integration tests.

## Task-Specific Shared Contract

- Integration tests exercise the full stack: database → API → worker → MCP server.
- A local FastMCP test server is started as a fixture, registered via the API, and referenced by an agent.
- Tests verify both happy paths (tool discovery + invocation) and error paths (unreachable server, disabled server, auth failures).
- Use the existing test server fixture pattern from `http_test_server.py` as a starting point.
- Integration tests should be runnable via `make test` or `make e2e-test` depending on infra requirements.

## Affected Component

- **Service/Module:** Integration Tests
- **File paths:**
  - `services/worker-service/tests/test_custom_tool_integration.py` (new — MCP session + executor integration)
  - `services/worker-service/tests/fixtures/custom_tool_test_server.py` (new — test MCP server with custom tools)
  - `services/api-service/src/test/java/com/persistentagent/api/controller/ToolServerControllerTest.java` (verify exists from Task 2 or create)
- **Change type:** new code

## Dependencies

- **Must complete first:** Task 1 (Database), Task 2 (API), Task 3 (Agent Config), Task 4 (Session Manager), Task 5 (Executor Integration)
- **Provides output to:** None (final task)
- **Shared interfaces/contracts:** All Track 4 interfaces

## Implementation Specification

### Step 1: Create a custom tool test MCP server

Create `services/worker-service/tests/fixtures/custom_tool_test_server.py`:

```python
"""A simple FastMCP test server exposing custom tools for integration tests."""

import asyncio
from mcp.server.fastmcp import FastMCP

def create_custom_tool_test_server(
    name: str = "test-tools",
    host: str = "127.0.0.1",
    port: int = 9100,
) -> FastMCP:
    """Create a FastMCP server with test tools for integration testing."""
    server = FastMCP(
        name=name,
        host=host,
        port=port,
    )

    @server.tool(
        name="echo",
        description="Echoes the input back. Useful for testing tool invocation.",
    )
    async def echo(message: str) -> str:
        """Echo the message back."""
        return f"Echo: {message}"

    @server.tool(
        name="add_numbers",
        description="Adds two numbers together.",
    )
    async def add_numbers(a: int, b: int) -> str:
        """Add two numbers."""
        return str(a + b)

    @server.tool(
        name="get_info",
        description="Returns structured information about a topic.",
    )
    async def get_info(topic: str, include_details: bool = False) -> str:
        """Get info about a topic."""
        result = f"Info about: {topic}"
        if include_details:
            result += " (with details)"
        return result

    return server


if __name__ == "__main__":
    """Run the test server as a standalone subprocess.

    Usage: python custom_tool_test_server.py --host 127.0.0.1 --port 9100
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()

    server = create_custom_tool_test_server(host=args.host, port=args.port)
    asyncio.run(server.run_streamable_http_async())
```

### Step 2: Create pytest fixture for the test server

Use the **subprocess pattern** from the existing `test_mcp_http_integration.py` — this avoids async fixture scope issues and provides better test isolation.

Add to the integration test file:

```python
import asyncio
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

WORKER_SERVICE_DIR = Path(__file__).resolve().parents[1]
PYTHON_BIN = Path(sys.executable)
CUSTOM_TOOL_SERVER_SCRIPT = WORKER_SERVICE_DIR / "tests" / "fixtures" / "custom_tool_test_server.py"


async def _wait_for_port(host: str, port: int, timeout_seconds: float = 5.0) -> None:
    """Wait until a TCP port is accepting connections."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.1)


# Each test class starts/stops its own server subprocess.
# This avoids async fixture scope issues entirely.
async def _start_test_server(host: str = "127.0.0.1", port: int = 9100):
    """Start the custom tool test server as a subprocess."""
    process = await asyncio.create_subprocess_exec(
        str(PYTHON_BIN), "-u", str(CUSTOM_TOOL_SERVER_SCRIPT),
        "--host", host, "--port", str(port),
        cwd=str(WORKER_SERVICE_DIR),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await _wait_for_port(host, port)
    return process, f"http://{host}:{port}/mcp"


async def _stop_test_server(process):
    """Stop the test server subprocess."""
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        process.kill()
```

### Step 3: Write MCP session manager integration tests

Create `services/worker-service/tests/test_custom_tool_integration.py`:

```python
"""Integration tests for custom tool (BYOT) lifecycle."""

import asyncio
import pytest

from executor.mcp_session import (
    McpConnectionError,
    McpSessionManager,
    McpToolCallError,
    ToolServerConfig,
)


class TestMcpSessionManagerIntegration:
    """Integration tests using a real local MCP server subprocess."""

    @pytest.mark.asyncio
    async def test_connect_and_discover_tools(self):
        """Connect to a real MCP server and discover its tools."""
        process, server_url = await _start_test_server(port=9100)
        try:
            config = ToolServerConfig(
                name="test-tools",
                url=server_url,
                auth_type="none",
            )
            manager = McpSessionManager()
            try:
                tools_by_server = await manager.connect([config])

                assert "test-tools" in tools_by_server
                tools = tools_by_server["test-tools"]
                tool_names = [t["name"] for t in tools]
                assert "echo" in tool_names
                assert "add_numbers" in tool_names
                assert "get_info" in tool_names

                # Verify tool schemas are present
                echo_tool = next(t for t in tools if t["name"] == "echo")
                assert echo_tool["description"] != ""
                assert "inputSchema" in echo_tool
            finally:
                await manager.close()
        finally:
            await _stop_test_server(process)

    @pytest.mark.asyncio
    async def test_call_tool_echo(self):
        """Invoke a tool on the MCP server and verify the result."""
        process, server_url = await _start_test_server(port=9101)
        try:
            config = ToolServerConfig(
                name="test-tools",
                url=server_url,
                auth_type="none",
            )
            manager = McpSessionManager()
            try:
                await manager.connect([config])
                result = await manager.call_tool("test-tools", "echo", {"message": "hello world"})
                assert "hello world" in str(result)
            finally:
                await manager.close()
        finally:
            await _stop_test_server(process)

    @pytest.mark.asyncio
    async def test_call_tool_add_numbers(self):
        """Invoke add_numbers tool and verify arithmetic result."""
        process, server_url = await _start_test_server(port=9102)
        try:
            config = ToolServerConfig(
                name="test-tools",
                url=server_url,
                auth_type="none",
            )
            manager = McpSessionManager()
            try:
                await manager.connect([config])
                result = await manager.call_tool("test-tools", "add_numbers", {"a": 3, "b": 7})
                assert "10" in str(result)
            finally:
                await manager.close()
        finally:
            await _stop_test_server(process)

    @pytest.mark.asyncio
    async def test_connect_unreachable_server(self):
        """Connecting to an unreachable server raises McpConnectionError."""
        config = ToolServerConfig(
            name="nonexistent",
            url="http://127.0.0.1:59999/mcp",
            auth_type="none",
        )
        manager = McpSessionManager(connect_timeout_seconds=2)
        with pytest.raises(McpConnectionError) as exc_info:
            await manager.connect([config])
        assert "nonexistent" in str(exc_info.value)
        await manager.close()

    @pytest.mark.asyncio
    async def test_connect_multiple_servers_one_fails(self):
        """If one server fails to connect, all sessions are cleaned up."""
        process, server_url = await _start_test_server(port=9103)
        try:
            good_config = ToolServerConfig(
                name="good-server",
                url=server_url,
                auth_type="none",
            )
            bad_config = ToolServerConfig(
                name="bad-server",
                url="http://127.0.0.1:59999/mcp",
                auth_type="none",
            )
            manager = McpSessionManager(connect_timeout_seconds=2)
            with pytest.raises(McpConnectionError):
                await manager.connect([good_config, bad_config])
            # All sessions should be cleaned up
            assert manager.connected_servers == []
            await manager.close()
        finally:
            await _stop_test_server(process)

    @pytest.mark.asyncio
    async def test_session_lifecycle_connect_close(self):
        """Session can be opened and closed cleanly."""
        process, server_url = await _start_test_server(port=9104)
        try:
            config = ToolServerConfig(
                name="test-tools",
                url=server_url,
                auth_type="none",
            )
            manager = McpSessionManager()
            await manager.connect([config])
            assert "test-tools" in manager.connected_servers
            await manager.close()
            assert manager.connected_servers == []
        finally:
            await _stop_test_server(process)


class TestSchemaConversionIntegration:
    """Integration tests for converting real MCP tool schemas to StructuredTool."""

    @pytest.mark.asyncio
    async def test_real_tool_schemas_convert(self):
        """Discover real tools and convert their schemas to StructuredTool."""
        from executor.schema_converter import mcp_tools_to_structured_tools

        process, server_url = await _start_test_server(port=9105)
        try:
            config = ToolServerConfig(
                name="test-tools",
                url=server_url,
                auth_type="none",
            )
            manager = McpSessionManager()
            try:
                tools_by_server = await manager.connect([config])
                schemas = tools_by_server["test-tools"]

                structured_tools = mcp_tools_to_structured_tools(
                    server_name="test-tools",
                    tool_schemas=schemas,
                    call_fn=manager.call_tool,
                )

                assert len(structured_tools) >= 3
                tool_names = [t.name for t in structured_tools]
                assert "test-tools__echo" in tool_names
                assert "test-tools__add_numbers" in tool_names
                assert "test-tools__get_info" in tool_names

                # Verify tools are invokable
                echo_tool = next(t for t in structured_tools if t.name == "test-tools__echo")
                result = await echo_tool.ainvoke({"message": "integration test"})
                assert "integration test" in str(result)
            finally:
                await manager.close()
        finally:
            await _stop_test_server(process)


class TestBearerAuthIntegration:
    """Integration tests for bearer token authentication.

    Tests that the session manager correctly sends auth headers.
    The test server does not validate tokens (no-auth), so we verify
    that providing a bearer token does not cause connection failures
    (positive test) and that the auth header is actually injected.
    """

    @pytest.mark.asyncio
    async def test_bearer_token_accepted_by_noauth_server(self):
        """A bearer token does not break connections to a no-auth server."""
        process, server_url = await _start_test_server(port=9106)
        try:
            config = ToolServerConfig(
                name="auth-test",
                url=server_url,
                auth_type="bearer_token",
                auth_token="test-token-abc123",
            )
            manager = McpSessionManager()
            try:
                tools_by_server = await manager.connect([config])
                assert "auth-test" in tools_by_server
                assert len(tools_by_server["auth-test"]) >= 3

                # Verify tool invocation also works with auth headers
                result = await manager.call_tool("auth-test", "echo", {"message": "auth test"})
                assert "auth test" in str(result)
            finally:
                await manager.close()
        finally:
            await _stop_test_server(process)

    @pytest.mark.asyncio
    async def test_no_auth_mode_sends_no_header(self):
        """In 'none' auth mode, no Authorization header is sent."""
        process, server_url = await _start_test_server(port=9107)
        try:
            config = ToolServerConfig(
                name="noauth-test",
                url=server_url,
                auth_type="none",
            )
            manager = McpSessionManager()
            try:
                tools_by_server = await manager.connect([config])
                assert "noauth-test" in tools_by_server
            finally:
                await manager.close()
        finally:
            await _stop_test_server(process)


class TestMixedToolsIntegration:
    """Integration tests for mixed built-in and custom tools."""

    @pytest.mark.asyncio
    async def test_tool_namespacing_no_collision(self):
        """Custom tools are namespaced and don't collide with built-in tool names."""
        from executor.schema_converter import mcp_tools_to_structured_tools
        from langchain_core.tools import StructuredTool
        from pydantic import BaseModel, Field

        # Simulate a built-in tool
        class CalcArgs(BaseModel):
            expression: str = Field(description="Math expression")

        async def calculator(expression: str) -> str:
            return "42"

        builtin_tool = StructuredTool.from_function(
            coroutine=calculator,
            name="calculator",
            description="A calculator",
            args_schema=CalcArgs,
        )

        # Get custom tools from MCP server
        process, server_url = await _start_test_server(port=9108)
        try:
            config = ToolServerConfig(
                name="test-tools",
                url=server_url,
                auth_type="none",
            )
            manager = McpSessionManager()
            try:
                tools_by_server = await manager.connect([config])
                custom_tools = mcp_tools_to_structured_tools(
                    server_name="test-tools",
                    tool_schemas=tools_by_server["test-tools"],
                    call_fn=manager.call_tool,
                )

                # Merge and verify no name collisions
                all_tools = [builtin_tool] + custom_tools
                tool_names = [t.name for t in all_tools]
                assert len(tool_names) == len(set(tool_names)), "Tool name collision detected"

                # Verify namespacing
                for tool in custom_tools:
                    assert "__" in tool.name, f"Custom tool {tool.name} is not namespaced"
                assert builtin_tool.name == "calculator"  # Built-in not namespaced
            finally:
                await manager.close()
        finally:
            await _stop_test_server(process)
```

### Step 4: Write API integration tests

Verify the API-layer tests from Task 2 exist and cover:

- `testCreateAndListToolServers` — POST then GET list
- `testCreateDuplicateName` — POST duplicate → 409/400
- `testGetToolServerMaskedToken` — GET detail → token masked
- `testUpdateToolServer` — PUT with partial update
- `testDeleteToolServer` — DELETE → 204, then GET → 404
- `testDiscoverTools` — POST discover (requires a running MCP server)

If these tests don't exist from Task 2, create them now.

### Step 5: Write console test scenarios

Document test scenarios for browser verification (to be executed manually or via Playwright):

1. **Tool Server CRUD Smoke Test:**
   - Navigate to `/tool-servers` → verify empty state
   - Click "Register Tool Server" → fill form (name: "test-tools", URL: "http://localhost:9100/mcp", auth: None) → submit
   - Verify server appears in list with "active" badge
   - Click server row → verify detail page shows server info
   - Click "Discover Tools" → verify echo, add_numbers, get_info appear
   - Edit: change status to "disabled" → save → verify badge changes to gray

2. **Agent Config with Tool Servers:**
   - Navigate to `/agents` → create agent
   - In the "Tool Servers" section, check "test-tools"
   - Submit → verify agent created with tool_servers in config
   - View agent detail → verify "test-tools" shown in read-only view
   - Edit agent → verify "test-tools" pre-checked → uncheck → save → verify removed

## Acceptance Criteria

- [ ] Custom tool test server fixture exists with echo, add_numbers, get_info tools
- [ ] `test_connect_and_discover_tools` — connects to real server, discovers 3+ tools
- [ ] `test_call_tool_echo` — invokes echo tool, gets correct result
- [ ] `test_call_tool_add_numbers` — invokes add_numbers tool, gets correct arithmetic
- [ ] `test_connect_unreachable_server` — raises `McpConnectionError`
- [ ] `test_connect_multiple_servers_one_fails` — cleans up all sessions on partial failure
- [ ] `test_real_tool_schemas_convert` — real MCP schemas convert to `StructuredTool` and are invokable
- [ ] `test_tool_namespacing_no_collision` — custom tools namespaced, no collision with built-ins
- [ ] `test_bearer_token_accepted_by_noauth_server` — bearer auth headers do not break connections, tool invocation works
- [ ] All integration tests pass with `make test`
- [ ] Console test scenarios documented and verified

## Testing Requirements

- **Integration tests:** All tests listed above must pass. Tests must start/stop the MCP test server fixture automatically.
- **Regression:** `make test` must pass with no regressions to existing tests.
- **Console:** Browser verification of CRUD smoke test and agent config scenarios.

## Constraints and Guardrails

- Use the existing `FastMCP` framework for the test server — do not introduce new test server frameworks.
- Use the **subprocess pattern** from `test_mcp_http_integration.py` for test server management — each test starts/stops its own subprocess. Do NOT use async fixtures with `scope="module"` as this causes pytest-asyncio event loop issues.
- Integration tests should be self-contained: start the test server, run tests, stop the server.
- Use different ports for each test to avoid port conflicts when tests run in parallel (ports 9100-9119 range).
- Do not require external services (databases, real MCP servers) for unit-level tests — mock where needed.
- Integration tests requiring a database should be in the `e2e-test` category, not blocking `make test`.
- Keep test server tools simple (echo, arithmetic) — do not create complex domain-specific tools.

## Assumptions

- Tasks 1-5 have been completed (database, API, agent config, session manager, executor integration).
- The `FastMCP` server's `streamable_http_app()` method returns a Starlette/ASGI app suitable for uvicorn.
- The test server fixture can run on `127.0.0.1:9100` without port conflicts.
- The worker virtualenv has all necessary dependencies for running integration tests.
- `make test` includes the `services/worker-service/tests/` directory in its test discovery.

<!-- AGENT_TASK_END: task-8-integration-tests.md -->
