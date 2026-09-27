"""Helpers for waiting on persisted task lifecycle state in integration tests."""

import asyncio
from typing import Any


TASK_STATUS_POLL_INTERVAL_SECONDS = 0.1
TASK_TERMINAL_TIMEOUT_SECONDS = 15.0
TERMINAL_TASK_STATUSES = {"completed", "dead_letter"}


async def wait_for_terminal_task(pool: Any, task_id: str) -> None:
    """Wait until a task reaches a terminal status, or fail with diagnostics."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TASK_TERMINAL_TIMEOUT_SECONDS

    while True:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT status, dead_letter_reason, dead_letter_error_details
                FROM tasks
                WHERE task_id=$1
                """,
                task_id,
            )

        if row is None:
            raise AssertionError(f"Task {task_id} disappeared while waiting for a terminal status")

        status = str(row["status"])
        if status in TERMINAL_TASK_STATUSES:
            return

        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(
                f"Task {task_id} did not reach a terminal status within "
                f"{TASK_TERMINAL_TIMEOUT_SECONDS:.0f}s; last status={status!r}, "
                f"dead_letter_reason={row['dead_letter_reason']!r}, "
                f"dead_letter_error_details={row['dead_letter_error_details']!r}"
            )

        await asyncio.sleep(min(TASK_STATUS_POLL_INTERVAL_SECONDS, remaining))
