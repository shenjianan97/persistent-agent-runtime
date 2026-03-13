"""Subprocess integration tests for the Phase 1 MCP server over HTTP."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


WORKER_SERVICE_DIR = Path(__file__).resolve().parents[1]
PYTHON_BIN = Path(sys.executable)
HTTP_SERVER_SCRIPT = WORKER_SERVICE_DIR / "tests" / "fixtures" / "http_test_server.py"


async def _wait_for_port(host: str, port: int, timeout_seconds: float = 5.0) -> None:
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


class TestMcpHttpIntegration:
    @pytest.mark.asyncio
    async def test_client_can_connect_to_local_http_server_and_call_tools(self) -> None:
        host = "127.0.0.1"
        port = 8765
        process = await asyncio.create_subprocess_exec(
            str(PYTHON_BIN),
            "-u",
            str(HTTP_SERVER_SCRIPT),
            "--host",
            host,
            "--port",
            str(port),
            cwd=str(WORKER_SERVICE_DIR),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            await _wait_for_port(host, port)
            url = f"http://{host}:{port}/mcp"
            async with streamable_http_client(url) as (
                read_stream,
                write_stream,
                _get_session_id,
            ):
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
        finally:
            process.terminate()
            await process.wait()
