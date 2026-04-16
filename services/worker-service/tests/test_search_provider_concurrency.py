"""Concurrency tests for DuckDuckGoSearchProvider.

The provider previously serialized all searches behind a threading.Lock to work
around a deadlock in ddgs < 9.12.1 where a process-wide shared ThreadPoolExecutor
would starve under concurrent callers. ddgs 9.12.1 moved each search onto its
own per-call executor, so the lock is no longer needed — but we still want
bounded concurrency to avoid triggering DuckDuckGo's per-IP rate limit.

These tests exercise the bounded-concurrency contract without touching the real
DDG network: a subclass replaces _do_search with an instrumented fake while the
base class's _search_sync continues to hold the thread-level semaphore.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from tools.errors import ToolTransportError
from tools.providers.search import DuckDuckGoSearchProvider, SearchResult


class _InstrumentedProvider(DuckDuckGoSearchProvider):
    """Records peak concurrent in-flight _do_search calls."""

    def __init__(self, *, sleep_seconds: float = 0.2, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sleep_seconds = sleep_seconds
        self._counter_lock = threading.Lock()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.completed = 0

    def _do_search(self, query: str, max_results: int) -> list[SearchResult]:
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
        """The legacy threading.Lock name must be gone; concurrency cap must be a
        thread-level semaphore so it stays held across asyncio.wait_for timeouts."""
        provider = DuckDuckGoSearchProvider()
        assert not hasattr(provider, "_lock"), (
            "legacy threading.Lock workaround should be removed"
        )
        # Must be a threading-layer semaphore, not asyncio.Semaphore, so the
        # worker thread holds the slot for its full lifetime.
        bounded_type = type(threading.BoundedSemaphore(1))
        assert isinstance(provider._semaphore, bounded_type), (
            "concurrency cap must be threading.BoundedSemaphore, not asyncio.Semaphore"
        )

    @pytest.mark.asyncio
    async def test_default_max_concurrent_is_bounded(self) -> None:
        """Default must leave enough headroom to avoid triggering DDG rate limits."""
        provider = DuckDuckGoSearchProvider()
        assert 1 < provider.max_concurrent <= 10

    @pytest.mark.asyncio
    async def test_semaphore_stays_held_after_asyncio_wait_for_timeout(self) -> None:
        """Regression for PR review: asyncio.wait_for timeout must NOT release
        the concurrency slot while the underlying thread is still running DDG
        work.

        Previously the provider used asyncio.Semaphore around asyncio.wait_for —
        when wait_for timed out the semaphore released immediately, allowing a
        new search to start while the orphaned thread kept hitting DDG. That
        defeated the rate-limit cap. The fix moves the semaphore inside the
        worker thread so the slot stays reserved until _do_search returns.
        """
        release = threading.Event()
        entered = threading.Event()

        class _HangingProvider(DuckDuckGoSearchProvider):
            def _do_search(self, query: str, max_results: int) -> list[SearchResult]:
                entered.set()
                release.wait(timeout=3.0)
                return []

        provider = _HangingProvider(max_concurrent=1, timeout_seconds=0.1)

        search_task = asyncio.create_task(provider.search("q1", 1))
        # Wait until the worker thread actually entered _do_search (→ semaphore acquired).
        await asyncio.to_thread(entered.wait, 1.0)
        assert entered.is_set(), "worker thread never entered _do_search"

        # asyncio.wait_for should time out; the caller sees a ToolTransportError.
        with pytest.raises(ToolTransportError):
            await search_task

        # The thread is still in _do_search. A new search must NOT be able to
        # acquire the semaphore, because that would allow simultaneous DDG
        # requests in excess of max_concurrent=1.
        acquired = provider._semaphore.acquire(blocking=False)
        try:
            assert not acquired, (
                "semaphore was released prematurely on wait_for timeout — "
                "orphaned threads could exceed the concurrency cap"
            )
        finally:
            if acquired:
                provider._semaphore.release()
            # Let the hanging thread finish so the test cleans up.
            release.set()
            await asyncio.sleep(0.2)
