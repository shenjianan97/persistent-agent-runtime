"""Search provider implementations for the Phase 1 web_search tool."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ddgs import DDGS

from tools.errors import ToolTransportError


# Cap concurrent searches from a single worker. Upstream ddgs >= 9.12.1 gives each
# call its own ThreadPoolExecutor, so we no longer need the process-wide lock that
# guarded the previously-shared executor. We still cap concurrency to avoid
# triggering DuckDuckGo's per-IP rate limit under bursty load.
DEFAULT_MAX_CONCURRENT_SEARCHES = 3


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    """Protocol for search backends used by the MCP server."""

    @property
    def provider_name(self) -> str:
        """Return the stable provider identifier."""

    async def search(self, query: str, max_results: int) -> Sequence[SearchResult]:
        """Run a search query and return normalized results."""


class DuckDuckGoSearchProvider:
    """DuckDuckGo-backed default implementation for web search."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_SEARCHES,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._timeout_seconds = timeout_seconds
        self.max_concurrent = max_concurrent
        # threading.BoundedSemaphore (not asyncio.Semaphore) because the real DDG
        # work runs in a worker thread via asyncio.to_thread. An asyncio semaphore
        # would release as soon as asyncio.wait_for times out, but the underlying
        # thread keeps running until _do_search returns — so new searches could
        # acquire a slot while orphaned threads are still hitting DDG, defeating
        # the rate-limit cap. Holding a thread-level semaphore inside the worker
        # thread keeps the slot reserved for the thread's actual lifetime.
        self._semaphore = threading.BoundedSemaphore(max_concurrent)

    @property
    def provider_name(self) -> str:
        return "duckduckgo"

    async def search(self, query: str, max_results: int) -> Sequence[SearchResult]:
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(self._search_sync, query, max_results),
                timeout=self._timeout_seconds,
            )
            return results
        except asyncio.TimeoutError as exc:
            raise ToolTransportError("Search request timed out.") from exc
        except Exception as exc:
            raise ToolTransportError(f"DuckDuckGo search failed: {str(exc)}") from exc

    def _search_sync(self, query: str, max_results: int) -> list[SearchResult]:
        with self._semaphore:
            return self._do_search(query, max_results)

    def _do_search(self, query: str, max_results: int) -> list[SearchResult]:
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, max_results=max_results))

        results = []
        for item in raw_results[:max_results]:
            url = str(item.get("href", "")).strip()
            if not url:
                continue
            results.append(
                SearchResult(
                    title=_trim_text(item.get("title"), fallback=url, limit=200),
                    url=url,
                    snippet=_trim_text(item.get("body"), fallback="", limit=600),
                )
            )
        return results


def _trim_text(value: object, *, fallback: str, limit: int) -> str:
    text = str(value or fallback).strip()
    text = " ".join(text.split())
    return text[:limit]
