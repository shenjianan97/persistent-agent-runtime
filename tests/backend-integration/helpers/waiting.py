import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


def wait_until_task_status(api_client: Any, task_id: str, status: str, timeout: float = 20.0) -> dict[str, Any]:
    return api_client.poll_until(task_id, status, timeout=timeout)


def wait_until_task_statuses(api_client: Any, task_id: str, statuses: set[str], timeout: float = 20.0) -> dict[str, Any]:
    return api_client.poll_for_statuses(task_id, statuses, timeout=timeout)


async def wait_until_task_status_async(
    api_client: Any,
    task_id: str,
    status: str,
    timeout: float = 20.0,
) -> dict[str, Any]:
    return await asyncio.to_thread(api_client.poll_until, task_id, status, timeout=timeout)


async def wait_until_task_statuses_async(
    api_client: Any,
    task_id: str,
    statuses: set[str],
    timeout: float = 20.0,
) -> dict[str, Any]:
    return await asyncio.to_thread(api_client.poll_for_statuses, task_id, statuses, timeout=timeout)


async def wait_for_async(
    check: Callable[[], Awaitable[Any]],
    *,
    timeout: float = 10.0,
    interval: float = 0.2,
    description: str = "condition",
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {description}; last value={last!r}")


async def wait_for_checkpoint_count(db: Any, task_id: str, min_count: int, timeout: float = 10.0) -> int:
    async def _check() -> int | None:
        count = await db.checkpoint_count(task_id)
        return count if count >= min_count else None

    return await wait_for_async(_check, timeout=timeout, description=f"checkpoint_count >= {min_count}")
