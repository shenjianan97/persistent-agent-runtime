"""Integration-style tests for the FastMCP Phase 1 server contract."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from tools.definitions import (
    TOOL_NAMES,
    ToolDependencies,
    get_tool_output_schema,
    get_tool_schema,
)
from tools.providers.search import SearchResult
from tools.read_url import ReadUrlResultData
from tools.server import SERVER_NAME, create_mcp_server


class _FakeSearchProvider:
    provider_name = "fake-search"

    async def search(self, query: str, max_results: int) -> Sequence[SearchResult]:
        del max_results
        return [
            SearchResult(
                title="Overview",
                url="https://example.com/overview",
                snippet=f"Result for {query}",
            )
        ]


class _FakeReadUrlFetcher:
    async def fetch(self, url: str, max_chars: int) -> ReadUrlResultData:
        del max_chars
        return ReadUrlResultData(
            final_url=url,
            title="Example",
            content="# Example\n\nReadable content.",
        )


def _extract_structured_result(result: object) -> dict[str, object]:
    if isinstance(result, tuple):
        return result[1]
    return result  # type: ignore[return-value]


class TestFastMcpServer:
    @pytest.mark.asyncio
    async def test_lists_exact_phase1_tools_with_stable_schemas(self) -> None:
        server = create_mcp_server(
            dependencies=ToolDependencies(
                search_provider=_FakeSearchProvider(),
                read_url_fetcher=_FakeReadUrlFetcher(),
            )
        )

        assert server.name == SERVER_NAME
        tools = await server.list_tools()

        assert [tool.name for tool in tools] == list(TOOL_NAMES)
        for tool in tools:
            assert tool.inputSchema == get_tool_schema(tool.name)
            assert tool.outputSchema == get_tool_output_schema(tool.name)

    @pytest.mark.asyncio
    async def test_calls_all_phase1_tools(self) -> None:
        server = create_mcp_server(
            dependencies=ToolDependencies(
                search_provider=_FakeSearchProvider(),
                read_url_fetcher=_FakeReadUrlFetcher(),
            )
        )

        search_result = _extract_structured_result(
            await server.call_tool("web_search", {"query": "persistent agents"})
        )
        read_result = _extract_structured_result(
            await server.call_tool("read_url", {"url": "https://example.com"})
        )
        calc_result = _extract_structured_result(
            await server.call_tool("calculator", {"expression": "1 + 2 * 3"})
        )

        assert search_result["provider"] == "fake-search"
        assert search_result["results"][0]["url"] == "https://example.com/overview"
        assert read_result["content"] == "# Example\n\nReadable content."
        assert calc_result == {"expression": "1 + 2 * 3", "result": 7}
