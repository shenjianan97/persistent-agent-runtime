"""Test-only HTTP MCP server fixture with deterministic dependencies."""

from __future__ import annotations

import argparse
import asyncio

import httpx

from tools.app import create_tool_server_app
from tools.definitions import ToolDependencies
from tools.providers.search import SearchResult
from tools.read_url import ReadUrlFetcher


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
                        <p>This content is served through the MCP HTTP integration test.</p>
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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    return parser


async def _run(host: str, port: int) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))
    dependencies = ToolDependencies(
        search_provider=_FixtureSearchProvider(),
        read_url_fetcher=ReadUrlFetcher(
            client=client,
            resolver=_public_resolver,
        ),
    )
    try:
        server = create_tool_server_app(
            dependencies=dependencies,
            host=host,
            port=port,
        )
        await server.run_streamable_http_async()
    finally:
        await client.aclose()


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    asyncio.run(_run(args.host, args.port))
