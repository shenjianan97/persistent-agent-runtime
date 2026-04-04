import asyncio
import contextlib
import importlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
E2E_ROOT = REPO_ROOT / "tests" / "backend-integration"

if str(E2E_ROOT) not in sys.path:
    sys.path.insert(0, str(E2E_ROOT))


def _reload_module(name: str):
    if name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


def test_api_client_request_uses_timeout(monkeypatch):
    api_client = _reload_module("helpers.api_client")

    captured: dict[str, object] = {}

    class _Response:
        status = 200

        def read(self) -> bytes:
            return b'{"status":"healthy"}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        captured["url"] = req.full_url
        return _Response()

    monkeypatch.setattr(api_client.urllib.request, "urlopen", _fake_urlopen)

    client = api_client.ApiClient("http://localhost:8080/v1")
    payload = client.health()

    assert payload["body"]["status"] == "healthy"
    assert captured["url"] == "http://localhost:8080/v1/health"
    assert captured["timeout"] == 10.0


def test_api_client_wraps_timeout_errors(monkeypatch):
    api_client = _reload_module("helpers.api_client")

    def _fake_urlopen(req, timeout=None):
        del req, timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(api_client.urllib.request, "urlopen", _fake_urlopen)

    client = api_client.ApiClient("http://localhost:8080/v1")

    with pytest.raises(RuntimeError, match="timed out"):
        client.get_task("task-123")


@pytest.mark.asyncio
async def test_db_pool_waits_for_api_recovery_after_force_clean(monkeypatch):
    e2e_conftest = _reload_module("conftest")

    calls: list[object] = []

    async def _fake_force_clean():
        calls.append("force_clean")

    def _fake_wait_for_api(base_url: str, timeout_sec: float = 120.0):
        calls.append(("wait_for_api", base_url, timeout_sec))

    class _FakePool:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    fake_pool = _FakePool()

    monkeypatch.setattr(e2e_conftest, "_force_clean", _fake_force_clean)
    monkeypatch.setattr(e2e_conftest, "_wait_for_api", _fake_wait_for_api)
    monkeypatch.setattr(e2e_conftest.asyncpg, "create_pool", AsyncMock(return_value=fake_pool))
    monkeypatch.setattr(e2e_conftest.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        e2e_conftest.asyncio,
        "all_tasks",
        lambda: {asyncio.current_task()},
    )

    agen = e2e_conftest.db_pool.__wrapped__(e2e_conftest.RuntimeHandles())
    returned_pool = await agen.__anext__()

    assert returned_pool is fake_pool
    assert calls == [
        "force_clean",
        ("wait_for_api", e2e_conftest.API_BASE, 30.0),
    ]

    with contextlib.suppress(StopAsyncIteration):
        await agen.__anext__()
    assert fake_pool.terminated is True


@pytest.mark.asyncio
async def test_force_clean_avoids_terminating_other_connections_when_clean_succeeds(monkeypatch):
    e2e_conftest = _reload_module("conftest")

    execute_calls: list[str] = []

    class _FakeConn:
        async def execute(self, sql: str):
            execute_calls.append(sql)

        async def close(self):
            return None

    fake_conn = _FakeConn()

    monkeypatch.setattr(e2e_conftest.asyncpg, "connect", AsyncMock(return_value=fake_conn))
    monkeypatch.setattr(e2e_conftest, "_do_clean", AsyncMock())
    monkeypatch.setattr(e2e_conftest, "_snapshot_db_activity", AsyncMock())
    monkeypatch.setattr(e2e_conftest.asyncio, "sleep", AsyncMock())

    await e2e_conftest._force_clean()

    assert execute_calls == []


@pytest.mark.asyncio
async def test_create_worker_uses_env_db_dsn_by_default(monkeypatch):
    monkeypatch.setenv("E2E_DB_DSN", "postgresql://postgres:postgres@localhost:5432/persistent_agent_runtime")
    worker_launcher = _reload_module("helpers.worker_launcher")

    fake_pool = SimpleNamespace()
    worker = await worker_launcher.create_worker(fake_pool, worker_id="worker-test")

    assert worker.config.db_dsn == "postgresql://postgres:postgres@localhost:5432/persistent_agent_runtime"
