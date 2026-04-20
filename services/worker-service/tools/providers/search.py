"""Search provider implementations for the Phase 1 web_search tool.

The default provider is Tavily (async-native HTTP API). We previously used
DuckDuckGo via the ``ddgs`` library but its global concurrency primitives
deadlock when two searches run from the same process — parallel
``asyncio.to_thread`` calls block BOTH worker threads AND the main event
loop, which starves heartbeats and reaper reconciliation and strands tasks
until a worker restart. See ``/tmp/repro_parallel.py``-style repro: two
concurrent ``DDGS().text()`` calls never return, heartbeat never ticks past
t=0. The ``ddgs 9.12.1`` release notes claimed per-call executors fixed
this but the freeze reproduced on ``ddgs 9.13.0``.

Tavily's async client uses ``httpx.AsyncClient`` against a REST API — no
shared locks, cooperates with asyncio naturally, and exposes an explicit
per-call timeout. Requires ``TAVILY_API_KEY`` — we fail-closed with a
clear error message rather than silently falling back, so misconfiguration
is visible on the first search attempt.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from tools.errors import ToolTransportError


DEFAULT_TAVILY_TIMEOUT_SECONDS = 15.0


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


class TavilySearchProvider:
    """Tavily-backed default implementation for web search.

    Uses ``tavily.AsyncTavilyClient`` directly — no thread pool wrapping, no
    shared concurrency primitives. The client opens an ``httpx.AsyncClient``
    per instance which cooperates with asyncio cleanly.

    Parameters
    ----------
    api_key:
        Optional explicit API key. Falls back to ``os.environ["TAVILY_API_KEY"]``.
        Missing key → ``ToolTransportError`` on the first ``search`` call
        (not at construction time — this keeps test/import paths that never
        actually search from blowing up before we can inject mocks).
    timeout_seconds:
        Per-call timeout enforced via ``asyncio.wait_for``. Applied on top of
        Tavily's own ``timeout=`` argument for belt-and-suspenders.
    search_depth:
        ``"basic"`` (default, 1 credit) or ``"advanced"`` (2 credits, richer
        content). Keep ``"basic"`` for routine agent searches — the agent
        can always request deeper reads by calling ``read_url`` on a hit.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TAVILY_TIMEOUT_SECONDS,
        search_depth: str = "basic",
    ) -> None:
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY") or ""
        self._timeout_seconds = timeout_seconds
        self._search_depth = search_depth
        # Lazily constructed — avoids hitting env/network at import time.
        self._client = None

    @property
    def provider_name(self) -> str:
        return "tavily"

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ToolTransportError(
                "web_search unavailable: TAVILY_API_KEY is not set. "
                "Add it to .env.localdev (see .env.localdev.example) or "
                "pass an explicit api_key to TavilySearchProvider."
            )
        # Import here so the hard dependency isn't required at import time
        # for tests / envs that stub the provider.
        from tavily import AsyncTavilyClient

        self._client = AsyncTavilyClient(api_key=self._api_key)
        return self._client

    async def search(self, query: str, max_results: int) -> Sequence[SearchResult]:
        client = self._ensure_client()
        try:
            payload = await asyncio.wait_for(
                client.search(
                    query=query,
                    max_results=max_results,
                    search_depth=self._search_depth,
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ToolTransportError("Search request timed out.") from exc
        except ToolTransportError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalise provider errors
            raise ToolTransportError(f"Tavily search failed: {exc}") from exc

        raw_results = (payload or {}).get("results") or []
        results: list[SearchResult] = []
        for item in raw_results[:max_results]:
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            results.append(
                SearchResult(
                    title=_trim_text(item.get("title"), fallback=url, limit=200),
                    url=url,
                    snippet=_trim_text(item.get("content"), fallback="", limit=600),
                )
            )
        return results


def _trim_text(value: object, *, fallback: str, limit: int) -> str:
    text = str(value or fallback).strip()
    text = " ".join(text.split())
    return text[:limit]
