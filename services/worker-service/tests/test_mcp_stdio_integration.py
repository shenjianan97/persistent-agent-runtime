"""Subprocess integration tests for the Phase 1 MCP server over stdio."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


WORKER_SERVICE_DIR = Path(__file__).resolve().parents[1]
PYTHON_BIN = Path(sys.executable)
STDIO_SERVER_SCRIPT = WORKER_SERVICE_DIR / "tests" / "fixtures" / "stdio_test_server.py"


class TestMcpStdioIntegration:
    @pytest.mark.asyncio
    async def test_client_can_connect_to_local_stdio_server_and_call_tools(self) -> None:
        server = StdioServerParameters(
            command=str(PYTHON_BIN),
            args=["-u", str(STDIO_SERVER_SCRIPT)],
            cwd=WORKER_SERVICE_DIR,
        )

        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init_result = await session.initialize()
                tools_result = await session.list_tools()

                assert init_result.serverInfo.name == "persistent-agent-runtime-tools"
                assert [tool.name for tool in tools_result.tools] == [
                    "web_search",
                    "read_url",
                    "calculator",
                ]

                calc_result = await session.call_tool(
                    "calculator",
                    {"expression": "2 + 3 * 4"},
                )
                assert calc_result.isError is False
                assert calc_result.structuredContent == {
                    "expression": "2 + 3 * 4",
                    "result": 14,
                }

                read_result = await session.call_tool(
                    "read_url",
                    {"url": "https://example.com/article", "max_chars": 500},
                )
                assert read_result.isError is False
                assert read_result.structuredContent == {
                    "final_url": "https://example.com/article",
                    "title": "Fixture Article",
                    "content": "# Fixture Article\n\nThis content is served through the MCP stdio integration test.",
                }

                search_result = await session.call_tool(
                    "web_search",
                    {"query": "durable execution", "max_results": 1},
                )
                assert search_result.isError is False
                assert search_result.structuredContent == {
                    "provider": "fixture-search",
                    "query": "durable execution",
                    "results": [
                        {
                            "title": "Fixture Result",
                            "url": "https://example.com/search-result",
                            "snippet": "Search result for durable execution",
                        }
                    ],
                }
