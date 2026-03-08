"""Tests for the Phase 1 read_url tool and URL fetcher."""

from __future__ import annotations

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tools.definitions import ToolDependencies
from tools.errors import ToolExecutionError, ToolInputError
from tools.providers.search import SearchResult
from tools.read_url import ReadUrlFetcher
from tools.server import create_mcp_server


class _UnusedSearchProvider:
    provider_name = "unused"

    async def search(self, query: str, max_results: int):  # pragma: no cover - defensive stub
        raise AssertionError(f"web_search should not be called in this test: {query}, {max_results}")


def _extract_structured_result(result: object) -> dict[str, object]:
    if isinstance(result, tuple):
        return result[1]
    return result  # type: ignore[return-value]


async def _public_resolver(host: str, port: int) -> list[str]:
    del host, port
    return ["93.184.216.34"]


async def _private_resolver(host: str, port: int) -> list[str]:
    del host, port
    return ["127.0.0.1"]


def _build_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestReadUrlFetcher:
    @pytest.mark.asyncio
    async def test_fetches_and_truncates_html_content(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            repeated_paragraph = "Durable execution keeps checkpointed state safe across crashes. "
            html = """
                <html>
                    <head><title>Example Page</title></head>
                    <body>
                        <main>
                            <h1>Example Page</h1>
                            <p>{paragraph}</p>
                            <script>alert('x')</script>
                        </main>
                    </body>
                </html>
            """.format(paragraph=repeated_paragraph * 12)
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=html,
            )

        fetcher = ReadUrlFetcher(
            client=_build_client(handler),
            resolver=_public_resolver,
        )
        server = create_mcp_server(
            dependencies=ToolDependencies(
                search_provider=_UnusedSearchProvider(),
                read_url_fetcher=fetcher,
            )
        )

        result = _extract_structured_result(
            await server.call_tool(
                "read_url",
                {"url": "https://example.com/article", "max_chars": 500},
            )
        )

        assert result["final_url"] == "https://example.com/article"
        assert result["title"] == "Example Page"
        assert result["content"].startswith("# Example Page")
        assert "[truncated]" in result["content"]

    @pytest.mark.asyncio
    async def test_rejects_private_targets(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, headers={"Content-Type": "text/plain"}, content="ok")

        fetcher = ReadUrlFetcher(
            client=_build_client(handler),
            resolver=_private_resolver,
        )
        server = create_mcp_server(
            dependencies=ToolDependencies(
                search_provider=_UnusedSearchProvider(),
                read_url_fetcher=fetcher,
            )
        )

        with pytest.raises(ToolError) as excinfo:
            await server.call_tool("read_url", {"url": "https://internal.example/test"})

        assert isinstance(excinfo.value.__cause__, ToolInputError)

    @pytest.mark.asyncio
    async def test_rejects_unsupported_content_types(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"%PDF-1.4",
            )

        fetcher = ReadUrlFetcher(
            client=_build_client(handler),
            resolver=_public_resolver,
        )

        with pytest.raises(ToolExecutionError):
            await fetcher.fetch("https://example.com/file.pdf", 5000)
