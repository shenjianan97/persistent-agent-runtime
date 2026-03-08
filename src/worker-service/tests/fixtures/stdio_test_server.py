"""Test-only stdio MCP server fixture with deterministic dependencies."""

from __future__ import annotations

import asyncio

import httpx

from tools.definitions import ToolDependencies
from tools.providers.search import SearchResult
from tools.read_url import ReadUrlFetcher
from tools.server import create_mcp_server


class _FixtureSearchProvider:
    provider_name = "fixture-search"

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        return [
            SearchResult(
                title="Fixture Result",
                url="https://example.com/search-result",
                snippet=f"Search result for {query}",
            )
        ][:max_results]


async def _public_resolver(host: str, port: int) -> list[str]:
    del host, port
    return ["93.184.216.34"]


def _mock_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) == "https://example.com/article":
        html = """
            <html>
                <head><title>Fixture Article</title></head>
                <body>
                    <main>
                        <h1>Fixture Article</h1>
                        <p>This content is served through the MCP stdio integration test.</p>
                    </main>
                </body>
            </html>
        """
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=html,
        )

    return httpx.Response(404, headers={"Content-Type": "text/plain"}, content="not found")


async def _run() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))
    dependencies = ToolDependencies(
        search_provider=_FixtureSearchProvider(),
        read_url_fetcher=ReadUrlFetcher(
            client=client,
            resolver=_public_resolver,
        ),
    )
    try:
        server = create_mcp_server(dependencies=dependencies)
        await server.run_stdio_async()
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(_run())
