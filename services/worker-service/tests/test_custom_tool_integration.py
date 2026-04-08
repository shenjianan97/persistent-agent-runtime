"""Integration tests for custom tool (BYOT) lifecycle."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from executor.mcp_session import (
    McpConnectionError,
    McpSessionManager,
    ToolServerConfig,
)


WORKER_SERVICE_DIR = Path(__file__).resolve().parents[1]
PYTHON_BIN = Path(sys.executable)
CUSTOM_TOOL_SERVER_SCRIPT = WORKER_SERVICE_DIR / "tests" / "fixtures" / "custom_tool_test_server.py"


async def _wait_for_port(host: str, port: int, timeout_seconds: float = 10.0) -> None:
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


async def _stop_test_server(process) -> None:
    """Stop the test server subprocess."""
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        process.kill()


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
