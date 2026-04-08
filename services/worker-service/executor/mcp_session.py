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
            if isinstance(result, BaseException):
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
                    "error_message": str(e),
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
