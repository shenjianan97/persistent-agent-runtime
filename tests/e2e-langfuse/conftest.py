"""
Shared fixtures for Langfuse E2E tests.

These tests require:
  - A running Langfuse instance (make test-langfuse-up)

The conftest auto-starts the API service (via gradlew bootRun) if it is not
already running, mirroring backend-integration/conftest.py behaviour.
"""

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
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
# API service auto-start helpers (mirrors backend-integration/conftest.py)
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, text=True, capture_output=True)


def _is_port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _is_api_healthy(base_url: str) -> bool:
    try:
        client = ApiClient(base_url)
        payload = client.health(raise_for_status=False)
        return payload["status_code"] == 200 and payload["body"].get("status") == "healthy"
    except Exception:
        return False


def _start_api_process() -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update({
        "DB_HOST": DB_HOST,
        "DB_PORT": str(DB_PORT),
        "DB_NAME": DB_NAME,
        "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD,
        "SERVER_PORT": str(API_PORT),
        "APP_DEV_TASK_CONTROLS_ENABLED": "true",
        "LANGFUSE_ENABLED": "false",
        "LANGFUSE_HOST": LANGFUSE_HOST,
        "LANGFUSE_PUBLIC_KEY": LANGFUSE_PUBLIC_KEY,
        "LANGFUSE_SECRET_KEY": LANGFUSE_SECRET_KEY,
    })
    log_file = REPO_ROOT / ".tmp" / "e2e-langfuse-api-service.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_file, "w", encoding="utf-8")
    process = subprocess.Popen(
        ["./gradlew", "bootRun"],
        cwd=str(REPO_ROOT / "services" / "api-service"),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    process._codex_log_handle = log_handle  # type: ignore[attr-defined]
    return process


def _wait_for_api(base_url: str, timeout_sec: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _is_api_healthy(base_url):
            return
        time.sleep(1.0)
    raise TimeoutError("API service did not become healthy in time")


@dataclass
class RuntimeHandles:
    started_api: bool = False
    api_process: subprocess.Popen[str] | None = None


@pytest.fixture(scope="session", autouse=True)
def runtime_environment() -> RuntimeHandles:
    handles = RuntimeHandles()

    if not _is_api_healthy(API_BASE):
        try:
            handles.api_process = _start_api_process()
            handles.started_api = True
            _wait_for_api(API_BASE)
        except Exception as exc:
            _cleanup_runtime(handles)
            pytest.skip(f"Failed to start API service: {exc}")

    yield handles
    _cleanup_runtime(handles)


def _cleanup_runtime(handles: RuntimeHandles) -> None:
    if handles.api_process and handles.started_api:
        process = handles.api_process
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log_handle = getattr(process, "_codex_log_handle", None)
        if log_handle:
            log_handle.close()


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
def api_client(platform_api_base: str, runtime_environment: RuntimeHandles) -> ApiClient:
    del runtime_environment
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
