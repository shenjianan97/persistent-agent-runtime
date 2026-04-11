<!-- AGENT_TASK_START: task-4-mcp-session-manager.md -->

# Task 4 — MCP Session Manager

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` — canonical design contract (Tool Discovery and Invocation, Session Manager sections)
2. `services/worker-service/tests/test_mcp_http_integration.py` — existing MCP HTTP client usage patterns with the SDK
3. `services/worker-service/tests/test_mcp_stdio_integration.py` — existing MCP stdio client usage patterns (reference only)
4. `services/worker-service/executor/graph.py` — current executor pattern, especially `_await_or_cancel()`
5. `services/worker-service/tools/definitions.py` — `ToolDefinition`, `ToolDependencies` dataclasses
6. `services/worker-service/pyproject.toml` — verify `mcp` SDK is already a dependency

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-4/progress.md` to "Done".

## Context

MCP sessions must remain open for the entire duration of task execution — from tool discovery through invocation. The nested `async with` pattern used in integration tests does not work here because the graph is built once and tool calls happen later during `ToolNode` dispatch.

Track 4 introduces an `McpSessionManager` that explicitly manages session lifetimes. It opens sessions to all referenced tool servers concurrently at task start, provides a `call_tool()` method for invocation during execution, and closes all sessions in a cleanup path.

The MCP SDK (`mcp==1.26.0`) is already a dependency. The `mcp.client.streamable_http.streamable_http_client()` transport is used for HTTP connections.

## Task-Specific Shared Contract

- Sessions are connection-per-task: opened at task start, closed on completion/pause/error.
- `McpSessionManager.connect()` opens sessions to all servers concurrently via `asyncio.gather()`.
- `McpSessionManager.call_tool()` invokes a tool on a specific server's session.
- `McpSessionManager.close()` closes all open sessions; safe to call multiple times.
- Auth tokens are injected via httpx client headers (Authorization: Bearer).
- Session open timeout: 30 seconds per server.
- Tool call timeout: 30 seconds per call (configurable via `call_timeout_seconds`).
- Response size limit: 1 MB per tool call result.
- Auth tokens must never appear in log messages.

## Affected Component

- **Service/Module:** Worker Service — MCP Session Management
- **File paths:**
  - `services/worker-service/executor/mcp_session.py` (new)
  - `services/worker-service/tests/test_mcp_session.py` (new)
- **Change type:** new code

## Dependencies

- **Must complete first:** Task 1 (Database Migration — need to know the `tool_servers` schema for config shape)
- **Provides output to:** Task 5 (Executor Integration — uses `McpSessionManager` in `execute_task()`)
- **Shared interfaces/contracts:** `McpSessionManager` class API, `ToolServerConfig` dataclass

## Implementation Specification

### Step 1: Define ToolServerConfig dataclass

Create `services/worker-service/executor/mcp_session.py`:

```python
"""MCP session manager for external tool server connections."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

RESPONSE_SIZE_LIMIT = 1_048_576  # 1 MB
DEFAULT_CALL_TIMEOUT_SECONDS = 30
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ToolServerConfig:
    """Configuration for connecting to an external MCP tool server."""
    name: str
    url: str
    auth_type: str  # "none" or "bearer_token"
    auth_token: str | None = None
```

### Step 2: Implement McpSessionManager

Continue in `services/worker-service/executor/mcp_session.py`:

```python
class _ServerSession:
    """Holds the resources for a single MCP server connection."""

    def __init__(self):
        self.http_client: httpx.AsyncClient | None = None
        self.transport_cm = None  # context manager from streamable_http_client
        self.session_cm = None    # context manager from ClientSession
        self.session: ClientSession | None = None
        self.read_stream = None
        self.write_stream = None


class McpSessionManager:
    """Manages MCP client sessions across graph execution lifetime.

    Usage:
        manager = McpSessionManager()
        try:
            tools_by_server = await manager.connect(server_configs)
            # ... build graph, execute ...
            result = await manager.call_tool("server-name", "tool-name", {"arg": "value"})
        finally:
            await manager.close()
    """

    def __init__(
        self,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ):
        self._sessions: dict[str, _ServerSession] = {}
        self._call_timeout = call_timeout_seconds
        self._connect_timeout = connect_timeout_seconds

    async def connect(
        self, servers: list[ToolServerConfig]
    ) -> dict[str, list[dict[str, Any]]]:
        """Open sessions to all servers concurrently, discover tools.

        Returns a dict mapping server name to list of tool schemas:
            {"server-name": [{"name": "tool", "description": "...", "inputSchema": {...}}, ...]}

        Raises McpConnectionError if any server fails to connect.
        """
        if not servers:
            return {}

        results = await asyncio.gather(
            *(self._connect_one(server) for server in servers),
            return_exceptions=True,
        )

        tools_by_server: dict[str, list[dict[str, Any]]] = {}
        for server, result in zip(servers, results):
            if isinstance(result, Exception):
                # Close any sessions that were successfully opened
                await self.close()
                raise McpConnectionError(
                    server_name=server.name,
                    server_url=server.url,
                    message=str(result),
                ) from result
            tools_by_server[server.name] = result

        return tools_by_server

    async def _connect_one(
        self, server: ToolServerConfig
    ) -> list[dict[str, Any]]:
        """Connect to a single MCP server and discover its tools."""
        sess = _ServerSession()

        headers = {}
        if server.auth_type == "bearer_token" and server.auth_token:
            headers["Authorization"] = f"Bearer {server.auth_token}"

        sess.http_client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(self._connect_timeout),
        )

        try:
            # Open streamable HTTP transport
            sess.transport_cm = streamable_http_client(
                server.url, http_client=sess.http_client
            )
            read_stream, write_stream, _ = await sess.transport_cm.__aenter__()
            sess.read_stream = read_stream
            sess.write_stream = write_stream

            # Open MCP client session
            sess.session_cm = ClientSession(read_stream, write_stream)
            sess.session = await sess.session_cm.__aenter__()

            # Initialize the session
            await asyncio.wait_for(
                sess.session.initialize(),
                timeout=self._connect_timeout,
            )

            # Discover tools
            tools_result = await asyncio.wait_for(
                sess.session.list_tools(),
                timeout=self._connect_timeout,
            )

            self._sessions[server.name] = sess

            logger.info(
                "mcp_session_opened",
                extra={
                    "server_name": server.name,
                    "server_url": server.url,
                    "tool_count": len(tools_result.tools),
                },
            )

            return [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                }
                for tool in tools_result.tools
            ]

        except Exception as e:
            logger.error(
                "mcp_session_error",
                extra={
                    "server_name": server.name,
                    "server_url": server.url,
                    "error_category": type(e).__name__,
                    "message": str(e),
                },
            )
            # Clean up partial connection on failure
            await self._close_session(server.name, sess, reason="error")
            raise

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        """Invoke a tool on a specific server's session.

        Raises:
            McpToolCallError: if the tool call fails or times out
            KeyError: if server_name is not connected
        """
        sess = self._sessions.get(server_name)
        if sess is None or sess.session is None:
            raise KeyError(f"No active session for server: {server_name}")

        import time as _time
        start_time = _time.monotonic()

        try:
            result = await asyncio.wait_for(
                sess.session.call_tool(tool_name, arguments),
                timeout=self._call_timeout,
            )
            duration_ms = int((_time.monotonic() - start_time) * 1000)

            # Check response size
            result_text = str(result)
            if len(result_text) > RESPONSE_SIZE_LIMIT:
                logger.warning(
                    "mcp_response_truncated",
                    extra={
                        "server_name": server_name,
                        "tool_name": tool_name,
                        "original_size": len(result_text),
                        "limit": RESPONSE_SIZE_LIMIT,
                    },
                )
                result_text = result_text[:RESPONSE_SIZE_LIMIT]

            logger.info(
                "mcp_tool_invoked",
                extra={
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "duration_ms": duration_ms,
                    "success": not result.isError if hasattr(result, "isError") else True,
                },
            )

            # Extract content from MCP CallToolResult
            # The SDK may return content via .content (list of content parts)
            # or .structuredContent (dict). Handle both.
            if hasattr(result, "structuredContent") and result.structuredContent:
                import json as _json
                return _json.dumps(result.structuredContent)

            if hasattr(result, "content") and result.content:
                # Return text content joined if multiple parts
                texts = []
                for part in result.content:
                    if hasattr(part, "text"):
                        texts.append(part.text)
                    else:
                        texts.append(str(part))
                return "\n".join(texts) if texts else str(result)

            return str(result)

        except asyncio.TimeoutError as e:
            logger.error(
                "mcp_tool_timeout",
                extra={
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "timeout_seconds": self._call_timeout,
                },
            )
            raise McpToolCallError(
                server_name=server_name,
                tool_name=tool_name,
                message=f"Tool call timed out after {self._call_timeout}s",
            ) from e
        except Exception as e:
            logger.error(
                "mcp_tool_error",
                extra={
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "error": str(e),
                },
            )
            raise McpToolCallError(
                server_name=server_name,
                tool_name=tool_name,
                message=str(e),
            ) from e

    async def close(self, reason: str = "completed") -> None:
        """Close all open sessions. Safe to call multiple times.

        Args:
            reason: Why the sessions are being closed. One of:
                    "completed", "paused", "error", "cleanup".
        """
        server_names = list(self._sessions.keys())
        for name in server_names:
            sess = self._sessions.pop(name, None)
            if sess:
                await self._close_session(name, sess, reason=reason)

    async def _close_session(self, name: str, sess: _ServerSession, *, reason: str = "cleanup") -> None:
        """Close a single server session, suppressing errors."""
        try:
            if sess.session_cm is not None:
                await sess.session_cm.__aexit__(None, None, None)
        except Exception as e:
            logger.debug("Error closing MCP session for %s: %s", name, e)

        try:
            if sess.transport_cm is not None:
                await sess.transport_cm.__aexit__(None, None, None)
        except Exception as e:
            logger.debug("Error closing transport for %s: %s", name, e)

        try:
            if sess.http_client is not None:
                await sess.http_client.aclose()
        except Exception as e:
            logger.debug("Error closing HTTP client for %s: %s", name, e)

        logger.info(
            "mcp_session_closed",
            extra={"server_name": name, "reason": reason},
        )

    @property
    def connected_servers(self) -> list[str]:
        """Return names of currently connected servers."""
        return list(self._sessions.keys())
```

### Step 3: Define error classes

Add at the bottom of `mcp_session.py`:

```python
class McpConnectionError(Exception):
    """Raised when an MCP server cannot be reached at discovery time."""

    def __init__(self, server_name: str, server_url: str, message: str):
        self.server_name = server_name
        self.server_url = server_url
        super().__init__(f"Failed to connect to MCP server '{server_name}' at {server_url}: {message}")


class McpToolCallError(Exception):
    """Raised when an MCP tool call fails or times out."""

    def __init__(self, server_name: str, tool_name: str, message: str):
        self.server_name = server_name
        self.tool_name = tool_name
        super().__init__(f"Tool call failed: {server_name}__{tool_name}: {message}")
```

### Step 4: Write unit tests

Create `services/worker-service/tests/test_mcp_session.py`:

```python
"""Unit tests for McpSessionManager."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from executor.mcp_session import (
    McpConnectionError,
    McpSessionManager,
    McpToolCallError,
    ToolServerConfig,
)


@pytest.fixture
def server_config():
    return ToolServerConfig(
        name="test-server",
        url="http://localhost:9000/mcp",
        auth_type="none",
    )


@pytest.fixture
def bearer_config():
    return ToolServerConfig(
        name="auth-server",
        url="http://localhost:9001/mcp",
        auth_type="bearer_token",
        auth_token="test-token-12345678",
    )


class TestToolServerConfig:
    def test_config_creation(self, server_config):
        assert server_config.name == "test-server"
        assert server_config.url == "http://localhost:9000/mcp"
        assert server_config.auth_type == "none"
        assert server_config.auth_token is None

    def test_config_with_bearer(self, bearer_config):
        assert bearer_config.auth_type == "bearer_token"
        assert bearer_config.auth_token == "test-token-12345678"

    def test_config_frozen(self, server_config):
        with pytest.raises(AttributeError):
            server_config.name = "changed"


class TestMcpSessionManagerConnect:
    @pytest.mark.asyncio
    async def test_connect_empty_servers(self):
        manager = McpSessionManager()
        result = await manager.connect([])
        assert result == {}
        await manager.close()

    @pytest.mark.asyncio
    async def test_connect_failure_raises_connection_error(self, server_config):
        manager = McpSessionManager(connect_timeout_seconds=1)
        with patch(
            "executor.mcp_session.streamable_http_client"
        ) as mock_transport:
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(side_effect=ConnectionRefusedError("refused"))
            mock_transport.return_value = mock_cm

            with pytest.raises(McpConnectionError) as exc_info:
                await manager.connect([server_config])

            assert "test-server" in str(exc_info.value)
            assert "refused" in str(exc_info.value)
        await manager.close()

    @pytest.mark.asyncio
    async def test_connect_sets_bearer_auth_header(self, bearer_config):
        manager = McpSessionManager()
        with patch("executor.mcp_session.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.aclose = AsyncMock()

            with patch(
                "executor.mcp_session.streamable_http_client"
            ) as mock_transport:
                mock_cm = AsyncMock()
                mock_cm.__aenter__ = AsyncMock(side_effect=ConnectionRefusedError("test"))
                mock_transport.return_value = mock_cm

                with pytest.raises(McpConnectionError):
                    await manager.connect([bearer_config])

                # Verify httpx client was created with auth headers
                mock_client_cls.assert_called_once()
                call_kwargs = mock_client_cls.call_args
                assert "Authorization" in call_kwargs.kwargs.get("headers", {})
                assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer test-token-12345678"

        await manager.close()


class TestMcpSessionManagerCallTool:
    @pytest.mark.asyncio
    async def test_call_tool_no_session_raises_key_error(self):
        manager = McpSessionManager()
        with pytest.raises(KeyError, match="No active session"):
            await manager.call_tool("nonexistent", "tool", {})

    @pytest.mark.asyncio
    async def test_call_tool_timeout_raises_error(self):
        manager = McpSessionManager(call_timeout_seconds=0.01)
        # Simulate a connected session with a slow tool call
        from executor.mcp_session import _ServerSession
        sess = _ServerSession()
        mock_session = AsyncMock()

        async def slow_call(*args, **kwargs):
            await asyncio.sleep(1)

        mock_session.call_tool = slow_call
        sess.session = mock_session
        manager._sessions["slow-server"] = sess

        with pytest.raises(McpToolCallError, match="timed out"):
            await manager.call_tool("slow-server", "slow-tool", {})

        await manager.close()


class TestMcpSessionManagerClose:
    @pytest.mark.asyncio
    async def test_close_empty_is_safe(self):
        manager = McpSessionManager()
        await manager.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_close_twice_is_safe(self):
        manager = McpSessionManager()
        await manager.close()
        await manager.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_connected_servers_empty(self):
        manager = McpSessionManager()
        assert manager.connected_servers == []


class TestMcpConnectionError:
    def test_error_message(self):
        err = McpConnectionError("my-server", "http://localhost:9000/mcp", "refused")
        assert "my-server" in str(err)
        assert "http://localhost:9000/mcp" in str(err)
        assert "refused" in str(err)
        assert err.server_name == "my-server"
        assert err.server_url == "http://localhost:9000/mcp"


class TestMcpToolCallError:
    def test_error_message(self):
        err = McpToolCallError("my-server", "my-tool", "timeout")
        assert "my-server__my-tool" in str(err)
        assert "timeout" in str(err)
        assert err.server_name == "my-server"
        assert err.tool_name == "my-tool"
```

## Acceptance Criteria

- [ ] `ToolServerConfig` dataclass exists with `name`, `url`, `auth_type`, `auth_token` fields
- [ ] `McpSessionManager.connect()` opens sessions concurrently via `asyncio.gather()`
- [ ] `connect()` returns a dict mapping server name to list of tool schemas
- [ ] `connect()` raises `McpConnectionError` if any server fails, and cleans up partial connections
- [ ] Bearer token auth is injected via httpx client headers
- [ ] `McpSessionManager.call_tool()` invokes a tool on the correct server session
- [ ] `call_tool()` respects timeout and raises `McpToolCallError` on timeout
- [ ] `call_tool()` enforces 1 MB response size limit with warning
- [ ] `McpSessionManager.close(reason)` closes all sessions with reason, safe to call multiple times
- [ ] `McpConnectionError` and `McpToolCallError` carry server/tool metadata
- [ ] Structured logging for `mcp_session_opened`, `mcp_tool_invoked` (with `duration_ms`), `mcp_session_closed` (with `reason`), `mcp_session_error`, `mcp_tool_timeout`, `mcp_tool_error`
- [ ] Auth tokens never appear in log messages
- [ ] All unit tests pass

## Testing Requirements

- **Unit tests:** Test config creation, connect with empty servers, connect failure, bearer auth header injection, call_tool with no session, call_tool timeout, close safety (empty, double-close), error class messages.
- **Integration tests:** (Covered by Task 8) Connect to a real local MCP server, discover tools, invoke a tool, verify result.

## Constraints and Guardrails

- Do not implement schema conversion to LangChain `StructuredTool` — Task 5 handles that.
- Do not modify `executor/graph.py` — Task 5 handles the integration.
- Do not add stdio transport support — Track 4 is HTTP-only.
- Do not persist session state or tool schemas — sessions are ephemeral per task execution.
- Use the existing `mcp` SDK dependency — do not add new dependencies.
- Auth tokens must not appear in any log message (use `server_name` and `server_url` for identification).

## Assumptions

- The `mcp` SDK version `1.26.0` is already installed in the worker virtualenv.
- `mcp.client.streamable_http.streamable_http_client()` returns a context manager yielding `(read_stream, write_stream, session_url)`.
- `ClientSession(read, write)` is a context manager that provides `initialize()`, `list_tools()`, and `call_tool()` methods.
- `list_tools()` returns an object with a `.tools` list, each tool having `.name`, `.description`, and `.inputSchema` attributes.
- `call_tool()` returns a `CallToolResult` with `.content` (list of content parts, each with `.text`) and `.isError` flag.
- The worker virtualenv is at `services/worker-service/.venv/` and should be used for running tests.

<!-- AGENT_TASK_END: task-4-mcp-session-manager.md -->
