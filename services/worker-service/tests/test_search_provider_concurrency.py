"""Concurrency tests for DuckDuckGoSearchProvider.

The provider previously serialized all searches behind a threading.Lock to work
around a deadlock in ddgs < 9.12.1 where a process-wide shared ThreadPoolExecutor
would starve under concurrent callers. ddgs 9.12.1 moved each search onto its
own per-call executor, so the lock is no longer needed — but we still want
bounded concurrency to avoid triggering DuckDuckGo's per-IP rate limit.

These tests exercise the bounded-concurrency contract without touching the real
DDG network: a subclass replaces _search_sync with an instrumented fake.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from tools.providers.search import DuckDuckGoSearchProvider, SearchResult


class _InstrumentedProvider(DuckDuckGoSearchProvider):
    """Records peak concurrent in-flight _search_sync calls."""

    def __init__(self, *, sleep_seconds: float = 0.2, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sleep_seconds = sleep_seconds
        self._counter_lock = threading.Lock()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.completed = 0

    def _search_sync(self, query: str, max_results: int) -> list[SearchResult]:
        with self._counter_lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            time.sleep(self._sleep_seconds)
            return [SearchResult(title=query, url=f"https://example.test/{query}", snippet="")]
        finally:
            with self._counter_lock:
                self.in_flight -= 1
                self.completed += 1


class TestSearchProviderConcurrency:
    @pytest.mark.asyncio
    async def test_semaphore_caps_in_flight_calls(self) -> None:
        max_concurrent = 3
        provider = _InstrumentedProvider(
            max_concurrent=max_concurrent,
            sleep_seconds=0.15,
            timeout_seconds=5.0,
        )

        results = await asyncio.gather(
            *[provider.search(f"q{i}", 1) for i in range(10)]
        )

        assert len(results) == 10
        assert provider.completed == 10
        assert provider.peak_in_flight <= max_concurrent, (
            f"peak concurrency {provider.peak_in_flight} exceeded cap {max_concurrent}"
        )
        # With 10 calls and 0.15s each at concurrency 3, we should actually
        # reach the cap rather than stay well below it.
        assert provider.peak_in_flight == max_concurrent, (
            f"semaphore under-utilized: peak={provider.peak_in_flight}, cap={max_concurrent}"
        )

    @pytest.mark.asyncio
    async def test_searches_run_in_parallel_up_to_cap(self) -> None:
        """With cap=5 and 5 concurrent 0.2s searches, wall clock ≈ 0.2s, not 1.0s."""
        provider = _InstrumentedProvider(
            max_concurrent=5,
            sleep_seconds=0.2,
            timeout_seconds=5.0,
        )

        start = time.monotonic()
        await asyncio.gather(*[provider.search(f"q{i}", 1) for i in range(5)])
        elapsed = time.monotonic() - start

        # Serial execution would take >= 5 * 0.2 = 1.0s. Parallel should be well under 0.5s.
        assert elapsed < 0.5, (
            f"5 concurrent searches took {elapsed:.2f}s; expected parallel execution"
        )

    @pytest.mark.asyncio
    async def test_no_threading_lock_on_provider(self) -> None:
        """Regression: the threading.Lock workaround must be replaced, not kept in addition."""
        provider = DuckDuckGoSearchProvider()
        assert not hasattr(provider, "_lock"), (
            "threading.Lock should be removed; use asyncio.Semaphore instead"
        )
        assert hasattr(provider, "_semaphore"), (
            "asyncio.Semaphore should gate concurrent searches"
        )

    @pytest.mark.asyncio
    async def test_default_max_concurrent_is_bounded(self) -> None:
        """Default must leave enough headroom to avoid triggering DDG rate limits."""
        provider = DuckDuckGoSearchProvider()
        # Sanity: semaphore exists and has a reasonable default bound
        # (not unbounded, not 1). Exact value is a product tuning knob.
        assert 1 < provider._semaphore._value <= 10  # type: ignore[attr-defined]
