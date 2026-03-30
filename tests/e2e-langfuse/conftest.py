"""
Shared fixtures for Langfuse E2E tests.

These tests require:
  - A running Langfuse instance (make test-langfuse-up)
  - The full platform stack (make start)

The conftest reuses the same E2EContext / infrastructure helpers from
tests/backend-integration so we don't duplicate worker/DB startup logic.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_SRC = REPO_ROOT / "services" / "worker-service"
BACKEND_INTEGRATION = REPO_ROOT / "tests" / "backend-integration"

# Make worker source and shared helpers importable
for _p in (str(WORKER_SRC), str(BACKEND_INTEGRATION)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from helpers.api_client import ApiClient
from helpers.db import DbHelper
from helpers.e2e_context import E2EContext
from helpers.mock_llm import DynamicChatProvider, simple_response
from helpers.worker_launcher import create_worker, stop_worker

# ---------------------------------------------------------------------------
# Environment / config
# ---------------------------------------------------------------------------

LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://127.0.0.1:3300")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "pk-lf-local")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "sk-lf-local")

DB_HOST = os.getenv("E2E_DB_HOST", "localhost")
DB_PORT = int(os.getenv("E2E_DB_PORT", "55432"))
DB_NAME = os.getenv("E2E_DB_NAME", "persistent_agent_runtime")
DB_USER = os.getenv("E2E_DB_USER", "postgres")
DB_PASSWORD = os.getenv("E2E_DB_PASSWORD", "postgres")
DB_DSN = os.getenv(
    "E2E_DB_DSN",
    f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
)

API_PORT = int(os.getenv("E2E_API_PORT", "8080"))
API_BASE = os.getenv("E2E_API_BASE", f"http://localhost:{API_PORT}/v1")

os.environ.setdefault("APP_DEV_TASK_CONTROLS_ENABLED", "true")


# ---------------------------------------------------------------------------
# Langfuse connectivity helpers
# ---------------------------------------------------------------------------


def _langfuse_basic_auth(public_key: str, secret_key: str) -> str:
    credentials = f"{public_key}:{secret_key}"
    return "Basic " + base64.b64encode(credentials.encode()).decode()


def langfuse_request(
    method: str,
    path: str,
    *,
    public_key: str = LANGFUSE_PUBLIC_KEY,
    secret_key: str = LANGFUSE_SECRET_KEY,
    host: str = LANGFUSE_HOST,
    payload: dict[str, Any] | None = None,
    expected_status: int | tuple[int, ...] = 200,
    raise_for_status: bool = True,
) -> dict[str, Any]:
    """Make an authenticated request to Langfuse's public REST API."""
    url = f"{host.rstrip('/')}{path}"
    headers = {
        "Authorization": _langfuse_basic_auth(public_key, secret_key),
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    expected = (expected_status,) if isinstance(expected_status, int) else expected_status

    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            result: dict[str, Any] = {"status_code": resp.status, "body": body}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"message": raw}
        result = {"status_code": exc.code, "body": body}

    if raise_for_status and result["status_code"] not in expected:
        raise RuntimeError(
            f"Langfuse request {method} {path} failed: status={result['status_code']} body={result['body']}"
        )
    return result


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def langfuse_host() -> str:
    return LANGFUSE_HOST


@pytest.fixture(scope="session")
def langfuse_credentials() -> dict[str, str]:
    return {"public_key": LANGFUSE_PUBLIC_KEY, "secret_key": LANGFUSE_SECRET_KEY}


@pytest.fixture(scope="session")
def platform_api_base() -> str:
    return API_BASE


@pytest.fixture
def api_client(platform_api_base: str) -> ApiClient:
    return ApiClient(platform_api_base)


@pytest_asyncio.fixture
async def db_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=8)
    # Clean slate for each test
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM checkpoint_writes")
        await conn.execute("DELETE FROM checkpoints")
        await conn.execute("DELETE FROM tasks")
        await conn.execute("DELETE FROM langfuse_endpoints")
    yield pool
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM checkpoint_writes")
        await conn.execute("DELETE FROM checkpoints")
        await conn.execute("DELETE FROM tasks")
        await conn.execute("DELETE FROM langfuse_endpoints")
    await pool.close()


@pytest_asyncio.fixture
async def db(db_pool: asyncpg.Pool) -> DbHelper:
    return DbHelper(db_pool)


@pytest.fixture
def llm_provider() -> DynamicChatProvider:
    provider = DynamicChatProvider(default_factory=lambda: simple_response("ok"))
    patcher = patch("executor.providers.create_llm", side_effect=provider.build)
    patcher.start()
    try:
        yield provider
    finally:
        patcher.stop()


class WorkerManager:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._workers: list[Any] = []

    async def start(self, **kwargs: Any) -> Any:
        worker = await create_worker(self._pool, **kwargs)
        await worker.start()
        self._workers.append(worker)
        return worker

    async def stop(self, worker: Any) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        await stop_worker(worker)

    async def stop_all(self) -> None:
        while self._workers:
            w = self._workers.pop()
            await stop_worker(w)


@pytest_asyncio.fixture
async def worker_manager(db_pool: asyncpg.Pool) -> WorkerManager:
    manager = WorkerManager(db_pool)
    try:
        yield manager
    finally:
        await manager.stop_all()


@pytest_asyncio.fixture
async def e2e(
    api_client: ApiClient,
    db: DbHelper,
    llm_provider: DynamicChatProvider,
    worker_manager: WorkerManager,
) -> E2EContext:
    """Unified E2E context — same pattern as backend-integration/conftest.py."""
    return E2EContext(api=api_client, db=db, llm=llm_provider, workers=worker_manager)
