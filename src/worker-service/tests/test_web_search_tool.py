"""Tests for the Phase 1 web_search MCP tool."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tools.definitions import ToolDependencies
from tools.errors import ToolTransportError
from tools.providers.search import SearchResult
from tools.server import create_mcp_server


class _FakeSearchProvider:
    provider_name = "fake-search"

    def __init__(self, results: Sequence[SearchResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, max_results: int) -> Sequence[SearchResult]:
        self.calls.append((query, max_results))
        return self.results[:max_results]


class _TransportFailingSearchProvider:
    provider_name = "fake-search"

    async def search(self, query: str, max_results: int) -> Sequence[SearchResult]:
        del query, max_results
        raise ToolTransportError("backend unavailable")


class _UnusedReadUrlFetcher:
    async def fetch(self, url: str, max_chars: int):  # pragma: no cover - defensive stub
        raise AssertionError(f"read_url should not be called in this test: {url}, {max_chars}")


def _extract_structured_result(result: object) -> dict[str, object]:
    if isinstance(result, tuple):
        return result[1]
    return result  # type: ignore[return-value]


class TestWebSearchTool:
    @pytest.mark.asyncio
    async def test_returns_structured_search_results(self) -> None:
        provider = _FakeSearchProvider(
            results=[
                SearchResult(
                    title="Result One",
                    url="https://example.com/1",
                    snippet="First result snippet.",
                ),
                SearchResult(
                    title="Result Two",
                    url="https://example.com/2",
                    snippet="Second result snippet.",
                ),
            ]
        )
        server = create_mcp_server(
            dependencies=ToolDependencies(
                search_provider=provider,
                read_url_fetcher=_UnusedReadUrlFetcher(),
            )
        )

        result = _extract_structured_result(
            await server.call_tool(
                "web_search",
                {"query": "persistent agents", "max_results": 1},
            )
        )

        assert provider.calls == [("persistent agents", 1)]
        assert result == {
            "provider": "fake-search",
            "query": "persistent agents",
            "results": [
                {
                    "title": "Result One",
                    "url": "https://example.com/1",
                    "snippet": "First result snippet.",
                }
            ],
        }

    @pytest.mark.asyncio
    async def test_surfaces_transport_failures_as_tool_errors(self) -> None:
        server = create_mcp_server(
            dependencies=ToolDependencies(
                search_provider=_TransportFailingSearchProvider(),
                read_url_fetcher=_UnusedReadUrlFetcher(),
            )
        )

        with pytest.raises(ToolError) as excinfo:
            await server.call_tool("web_search", {"query": "persistent agents"})

        assert isinstance(excinfo.value.__cause__, ToolTransportError)
