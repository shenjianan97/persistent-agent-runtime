"""Search provider implementations for the Phase 1 web_search tool."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from tools.env import load_worker_env
from tools.errors import ToolExecutionError, ToolTransportError


TAVILY_SEARCH_URL = "https://api.tavily.com/search"


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
    """Tavily-backed default implementation for web search."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str = TAVILY_SEARCH_URL,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        load_worker_env()
        self._api_key = api_key or os.getenv("TAVILY_API_KEY")
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def provider_name(self) -> str:
        return "tavily"

    async def search(self, query: str, max_results: int) -> Sequence[SearchResult]:
        if not self._api_key:
            raise ToolExecutionError("TAVILY_API_KEY is not configured.")

        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_images": False,
            "include_raw_content": False,
        }
        headers = {"Content-Type": "application/json"}

        try:
            response = await self._post(payload, headers)
        except httpx.TimeoutException as exc:
            raise ToolTransportError("Search request timed out.") from exc
        except httpx.HTTPError as exc:
            raise ToolTransportError("Search provider request failed.") from exc

        if response.status_code in {408, 429} or response.status_code >= 500:
            raise ToolTransportError(
                f"Search provider temporarily failed with status {response.status_code}."
            )
        if response.status_code >= 400:
            raise ToolExecutionError(
                f"Search provider rejected the request with status {response.status_code}."
            )

        data = response.json()
        results: list[SearchResult] = []
        for item in data.get("results", [])[:max_results]:
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            results.append(
                SearchResult(
                    title=_trim_text(item.get("title"), fallback=url, limit=200),
                    url=url,
                    snippet=_trim_text(
                        item.get("content") or item.get("snippet"),
                        fallback="",
                        limit=600,
                    ),
                )
            )
        return results

    async def _post(self, payload: dict[str, object], headers: dict[str, str]) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(
                self._endpoint,
                json=payload,
                headers=headers,
                timeout=self._timeout_seconds,
            )

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            return await client.post(self._endpoint, json=payload, headers=headers)


def _trim_text(value: object, *, fallback: str, limit: int) -> str:
    text = str(value or fallback).strip()
    text = " ".join(text.split())
    return text[:limit]
