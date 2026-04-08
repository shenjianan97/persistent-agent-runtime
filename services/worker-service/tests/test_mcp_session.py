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
