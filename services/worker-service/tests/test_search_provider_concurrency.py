"""Concurrency + normalization tests for TavilySearchProvider.

Tavily is async-native via ``AsyncTavilyClient`` (httpx under the hood), so
unlike the previous ``ddgs`` provider there are no thread pools, no global
locks, and no shared executors — parallel searches are just concurrent
awaits on independent HTTP requests.

These tests stub the Tavily client so they do not hit the network.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from tools.errors import ToolTransportError
from tools.providers.search import SearchResult, TavilySearchProvider


class _FakeAsyncTavilyClient:
    """Records parallel in-flight calls and returns a canned Tavily payload."""

    def __init__(self, *, sleep_seconds: float = 0.2) -> None:
        self._sleep_seconds = sleep_seconds
        self._counter_lock = asyncio.Lock()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.completed = 0

    async def search(
        self, query: str, max_results: int = 5, **_kwargs: Any
    ) -> dict[str, Any]:
        async with self._counter_lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._sleep_seconds)
            return {
                "query": query,
                "results": [
                    {
                        "title": f"title-{query}",
                        "url": f"https://example.test/{query}",
                        "content": f"snippet-{query}",
                        "score": 0.9,
                    }
                ],
            }
        finally:
            async with self._counter_lock:
                self.in_flight -= 1
                self.completed += 1


def _inject(provider: TavilySearchProvider, fake: Any) -> None:
    # Bypass the env-var guard and API-key check by wiring the fake directly.
    provider._client = fake  # type: ignore[attr-defined]
    provider._api_key = "test-key-not-used"  # type: ignore[attr-defined]


class TestTavilySearchProviderConcurrency:
    @pytest.mark.asyncio
    async def test_searches_run_in_parallel(self) -> None:
        """Two concurrent searches should overlap on the async loop — wall
        clock ~= single-call duration, not 2× it. This is the core
        regression check against the ddgs freeze that prompted the switch:
        under the old provider, two parallel calls would deadlock the main
        thread for minutes; under Tavily they should simply coexist."""
        fake = _FakeAsyncTavilyClient(sleep_seconds=0.2)
        provider = TavilySearchProvider()
        _inject(provider, fake)

        start = time.monotonic()
        results = await asyncio.gather(
            provider.search("q1", 5),
            provider.search("q2", 5),
        )
        elapsed = time.monotonic() - start

        assert len(results) == 2
        assert fake.completed == 2
        assert fake.peak_in_flight == 2, (
            "expected both Tavily calls to be in flight concurrently; "
            f"peak={fake.peak_in_flight}"
        )
        # Serial would be ~0.4s; parallel should be ~0.2s with generous slack.
        assert elapsed < 0.35, (
            f"2 concurrent searches took {elapsed:.2f}s; expected parallel execution"
        )

    @pytest.mark.asyncio
    async def test_normalizes_tavily_results_to_search_result(self) -> None:
        fake = _FakeAsyncTavilyClient(sleep_seconds=0.0)
        provider = TavilySearchProvider()
        _inject(provider, fake)

        out = await provider.search("hello", 3)
        assert len(out) == 1
        item = out[0]
        assert isinstance(item, SearchResult)
        assert item.title == "title-hello"
        assert item.url == "https://example.test/hello"
        assert item.snippet == "snippet-hello"

    @pytest.mark.asyncio
    async def test_drops_results_with_empty_url(self) -> None:
        class _NoUrlClient:
            async def search(self, query: str, max_results: int = 5, **_: Any):
                return {
                    "results": [
                        {"title": "no-url", "url": "", "content": "x"},
                        {
                            "title": "keep",
                            "url": "https://example.test/k",
                            "content": "y",
                        },
                    ]
                }

        provider = TavilySearchProvider()
        _inject(provider, _NoUrlClient())
        out = await provider.search("hello", 5)
        assert len(out) == 1
        assert out[0].url == "https://example.test/k"

    @pytest.mark.asyncio
    async def test_timeout_raises_tool_transport_error(self) -> None:
        class _HangingClient:
            async def search(self, query: str, max_results: int = 5, **_: Any):
                await asyncio.sleep(5.0)
                return {"results": []}

        provider = TavilySearchProvider(timeout_seconds=0.1)
        _inject(provider, _HangingClient())

        with pytest.raises(ToolTransportError, match="timed out"):
            await provider.search("hang", 1)

    @pytest.mark.asyncio
    async def test_provider_error_is_wrapped_in_tool_transport_error(self) -> None:
        class _ExplodingClient:
            async def search(self, query: str, max_results: int = 5, **_: Any):
                raise RuntimeError("tavily boom")

        provider = TavilySearchProvider()
        _inject(provider, _ExplodingClient())

        with pytest.raises(ToolTransportError, match="tavily boom"):
            await provider.search("boom", 1)
