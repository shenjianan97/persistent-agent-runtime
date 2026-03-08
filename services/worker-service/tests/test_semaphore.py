"""Tests for semaphore bounding of concurrent tasks."""

import asyncio

import pytest

from core.config import WorkerConfig


class TestSemaphoreBounding:
    """Verify that concurrency is bounded by asyncio.Semaphore(MAX_CONCURRENT_TASKS)."""

    async def test_semaphore_initial_value(self):
        config = WorkerConfig(max_concurrent_tasks=10)
        sem = asyncio.Semaphore(config.max_concurrent_tasks)
        # All 10 slots should be available
        for _ in range(10):
            assert not sem.locked()
            await sem.acquire()
        # Now it should be locked
        assert sem.locked()

    async def test_semaphore_blocks_at_max(self):
        config = WorkerConfig(max_concurrent_tasks=3)
        sem = asyncio.Semaphore(config.max_concurrent_tasks)

        acquired = 0
        for _ in range(3):
            await sem.acquire()
            acquired += 1

        assert acquired == 3
        assert sem.locked()

        # Trying to acquire should block — verify with wait_for
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sem.acquire(), timeout=0.05)

    async def test_semaphore_release_allows_new_acquisition(self):
        config = WorkerConfig(max_concurrent_tasks=2)
        sem = asyncio.Semaphore(config.max_concurrent_tasks)

        await sem.acquire()
        await sem.acquire()
        assert sem.locked()

        sem.release()
        assert not sem.locked()

        # Should succeed now
        await asyncio.wait_for(sem.acquire(), timeout=0.05)
        assert sem.locked()

    async def test_concurrent_tasks_bounded(self):
        """Simulate concurrent task execution with semaphore bounding."""
        max_tasks = 5
        sem = asyncio.Semaphore(max_tasks)
        peak_concurrent = 0
        current_concurrent = 0

        async def fake_task(task_num: int) -> None:
            nonlocal peak_concurrent, current_concurrent
            async with sem:
                current_concurrent += 1
                peak_concurrent = max(peak_concurrent, current_concurrent)
                await asyncio.sleep(0.01)  # Simulate work
                current_concurrent -= 1

        # Launch more tasks than max
        tasks = [asyncio.create_task(fake_task(i)) for i in range(20)]
        await asyncio.gather(*tasks)

        assert peak_concurrent <= max_tasks
        assert current_concurrent == 0

    async def test_custom_max_concurrent(self):
        for max_val in [1, 5, 20, 100]:
            config = WorkerConfig(max_concurrent_tasks=max_val)
            sem = asyncio.Semaphore(config.max_concurrent_tasks)
            for _ in range(max_val):
                await sem.acquire()
            assert sem.locked()
