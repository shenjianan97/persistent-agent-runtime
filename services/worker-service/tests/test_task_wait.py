import pytest

from tests import task_wait


class _FakeConnection:
    def __init__(self, rows):
        self._rows = iter(rows)

    async def fetchrow(self, *_args):
        return next(self._rows)


class _FakeAcquire:
    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *_args):
        return None


class _FakePool:
    def __init__(self, rows):
        self._connection = _FakeConnection(rows)

    def acquire(self):
        return _FakeAcquire(self._connection)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "dead_letter"])
async def test_wait_for_terminal_task_accepts_terminal_status(terminal_status):
    pool = _FakePool(
        [
            {
                "status": "running",
                "dead_letter_reason": None,
                "dead_letter_error_details": None,
            },
            {
                "status": terminal_status,
                "dead_letter_reason": None,
                "dead_letter_error_details": None,
            },
        ]
    )

    await task_wait.wait_for_terminal_task(pool, "task-1")


@pytest.mark.asyncio
async def test_wait_for_terminal_task_reports_last_status_on_timeout(monkeypatch):
    monkeypatch.setattr(task_wait, "TASK_STATUS_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(task_wait, "TASK_TERMINAL_TIMEOUT_SECONDS", 0.01)
    pool = _FakePool(
        [
            {
                "status": "running",
                "dead_letter_reason": None,
                "dead_letter_error_details": None,
            }
        ]
        * 20
    )

    with pytest.raises(AssertionError, match="last status='running'"):
        await task_wait.wait_for_terminal_task(pool, "task-2")
