import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_SRC = REPO_ROOT / "services" / "worker-service"
E2E_ROOT = REPO_ROOT / "tests" / "backend-integration"
if str(WORKER_SRC) not in sys.path:
    sys.path.insert(0, str(WORKER_SRC))
if str(E2E_ROOT) not in sys.path:
    sys.path.insert(0, str(E2E_ROOT))

from helpers.api_client import ApiClient
from helpers.db import DbHelper
from helpers.e2e_context import E2EContext
from helpers.mock_llm import DynamicChatProvider, simple_response
from helpers.worker_launcher import create_worker, stop_worker

MIGRATIONS_DIR = REPO_ROOT / "infrastructure" / "database" / "migrations"

DB_HOST = os.getenv("E2E_DB_HOST", "localhost")
DB_PORT = int(os.getenv("E2E_DB_PORT", "55433"))
DB_NAME = os.getenv("E2E_DB_NAME", "persistent_agent_runtime_e2e")
DB_USER = os.getenv("E2E_DB_USER", "postgres")
DB_PASSWORD = os.getenv("E2E_DB_PASSWORD", "postgres")
DB_DSN = os.getenv(
    "E2E_DB_DSN",
    f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
)

API_PORT = int(os.getenv("E2E_API_PORT", "8081"))
API_BASE = os.getenv("E2E_API_BASE", f"http://localhost:{API_PORT}/v1")

os.environ.setdefault("APP_DEV_TASK_CONTROLS_ENABLED", "true")

PG_CONTAINER = os.getenv("E2E_PG_CONTAINER", "par-e2e-postgres")
PG_IMAGE = os.getenv("E2E_PG_IMAGE", "postgres:16")


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=True,
    )


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


def _docker_container_exists(name: str) -> bool:
    proc = _run(["docker", "ps", "-a", "--format", "{{.Names}}"], check=True)
    names = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return name in names


def _docker_container_running(name: str) -> bool:
    proc = _run(["docker", "ps", "--format", "{{.Names}}"], check=True)
    names = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return name in names


def _wait_for_postgres(container_name: str, timeout_sec: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        proc = _run(["docker", "exec", container_name, "pg_isready", "-U", DB_USER], check=False)
        if proc.returncode == 0:
            return
        time.sleep(0.5)
    raise TimeoutError("PostgreSQL did not become ready in time")


async def _schema_exists() -> bool:
    conn = await asyncpg.connect(DB_DSN)
    try:
        regclass = await conn.fetchval("SELECT to_regclass('public.tasks')")
        return regclass is not None
    finally:
        await conn.close()


def _apply_migrations() -> None:
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        _run(["psql", DB_DSN, "-f", str(sql_file)], check=True)


def _start_api_process() -> subprocess.Popen[str]:
    env = os.environ.copy()
    langfuse_enabled = os.getenv("E2E_LANGFUSE_ENABLED", os.getenv("LANGFUSE_ENABLED", "false"))
    env.update(
        {
            "DB_HOST": DB_HOST,
            "DB_PORT": str(DB_PORT),
            "DB_NAME": DB_NAME,
            "DB_USER": DB_USER,
            "DB_PASSWORD": DB_PASSWORD,
            "SERVER_PORT": str(API_PORT),
            "APP_DEV_TASK_CONTROLS_ENABLED": "true",
            "LANGFUSE_ENABLED": langfuse_enabled,
            "LANGFUSE_HOST": os.getenv("E2E_LANGFUSE_HOST", os.getenv("LANGFUSE_HOST", "http://127.0.0.1:3300")),
            "LANGFUSE_PUBLIC_KEY": os.getenv("E2E_LANGFUSE_PUBLIC_KEY", os.getenv("LANGFUSE_PUBLIC_KEY", "pk-lf-local")),
            "LANGFUSE_SECRET_KEY": os.getenv("E2E_LANGFUSE_SECRET_KEY", os.getenv("LANGFUSE_SECRET_KEY", "sk-lf-local")),
        }
    )
    log_file = REPO_ROOT / ".tmp" / "e2e-api-service.log"
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


def pytest_configure() -> None:
    worker_path = str(WORKER_SRC)
    helpers_path = str(E2E_ROOT)
    if worker_path not in sys.path:
        sys.path.insert(0, worker_path)
    if helpers_path not in sys.path:
        sys.path.insert(0, helpers_path)


@dataclass
class RuntimeHandles:
    started_postgres: bool = False
    started_api: bool = False
    postgres_was_running: bool = False
    api_process: subprocess.Popen[str] | None = None


@pytest.fixture(scope="session", autouse=True)
def runtime_environment() -> RuntimeHandles:
    if os.getenv("E2E_SKIP_AUTO_INFRA", "0") == "1":
        return RuntimeHandles()

    handles = RuntimeHandles()

    postgres_reachable = _is_port_open(DB_HOST, DB_PORT)
    if not postgres_reachable:
        if not _docker_container_exists(PG_CONTAINER):
            _run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    PG_CONTAINER,
                    "-e",
                    f"POSTGRES_USER={DB_USER}",
                    "-e",
                    f"POSTGRES_PASSWORD={DB_PASSWORD}",
                    "-e",
                    f"POSTGRES_DB={DB_NAME}",
                    "-p",
                    f"{DB_PORT}:5432",
                    PG_IMAGE,
                ],
                check=True,
            )
            handles.started_postgres = True
        else:
            handles.postgres_was_running = _docker_container_running(PG_CONTAINER)
            if not handles.postgres_was_running:
                _run(["docker", "start", PG_CONTAINER], check=True)
                handles.started_postgres = True

        _wait_for_postgres(PG_CONTAINER)

    try:
        if not asyncio_run(_schema_exists()):
            _apply_migrations()
    except Exception as exc:  # pragma: no cover - startup failure path
        pytest.skip(f"Failed to verify/apply schema: {exc}")

    if not _is_api_healthy(API_BASE):
        try:
            handles.api_process = _start_api_process()
            handles.started_api = True
            _wait_for_api(API_BASE)
        except Exception as exc:  # pragma: no cover - startup failure path
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

    if handles.started_postgres and not handles.postgres_was_running:
        _run(["docker", "stop", PG_CONTAINER], check=False)


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def _emit_cleanup_log(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    print(json.dumps(payload, default=str), file=sys.stderr, flush=True)


async def _snapshot_db_activity(reason: str, attempt: int) -> None:
    try:
        conn = await asyncpg.connect(DB_DSN, timeout=2.0, command_timeout=2.0)
    except Exception as exc:
        _emit_cleanup_log(
            "e2e_cleanup_snapshot_failed",
            reason=reason,
            attempt=attempt,
            error=str(exc),
        )
        return

    try:
        rows = await conn.fetch(
            """
            SELECT pid,
                   state,
                   wait_event_type,
                   wait_event,
                   left(query, 200) AS query,
                   pg_blocking_pids(pid) AS blocking_pids
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
            ORDER BY xact_start NULLS LAST, query_start NULLS LAST
            """
        )
        _emit_cleanup_log(
            "e2e_cleanup_snapshot",
            reason=reason,
            attempt=attempt,
            sessions=[dict(row) for row in rows],
        )
    except Exception as exc:
        _emit_cleanup_log(
            "e2e_cleanup_snapshot_failed",
            reason=reason,
            attempt=attempt,
            error=str(exc),
        )
    finally:
        await conn.close()


async def _force_clean() -> None:
    """Terminate all other connections, wait for locks to release, then clean tables.

    Retries the cleanup with escalating waits to handle slow CI runners where
    PostgreSQL backends take time to release row locks after termination.
    """
    for attempt in range(3):
        try:
            _emit_cleanup_log("e2e_force_clean_attempt_started", attempt=attempt + 1)
            # Try a direct cleanup first so healthy runs do not disrupt the shared API pool.
            conn = await asyncpg.connect(DB_DSN)
            try:
                await asyncio.wait_for(
                    _do_clean(conn), timeout=5.0,
                )
            finally:
                await conn.close()

            _emit_cleanup_log("e2e_force_clean_attempt_succeeded", attempt=attempt + 1)
            return  # success
        except (asyncio.TimeoutError, Exception) as exc:
            _emit_cleanup_log(
                "e2e_force_clean_attempt_failed",
                attempt=attempt + 1,
                error=repr(exc),
            )
            await _snapshot_db_activity(reason=type(exc).__name__, attempt=attempt + 1)
            if attempt == 2:
                raise
            # Escalate only after a failed direct cleanup by terminating the
            # remaining sessions that may still be holding locks.
            conn = await asyncpg.connect(DB_DSN)
            try:
                await conn.execute("""
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                """)
            finally:
                await conn.close()
            await asyncio.sleep(1.0)


async def _do_clean(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM agent_cost_ledger")
    await conn.execute("DELETE FROM task_events")
    await conn.execute("DELETE FROM checkpoint_writes")
    await conn.execute("DELETE FROM checkpoints")
    await conn.execute("DELETE FROM tasks")
    await conn.execute("DELETE FROM agent_runtime_state")
    await conn.execute("DELETE FROM agents")


@pytest_asyncio.fixture
async def db_pool(runtime_environment: RuntimeHandles) -> asyncpg.Pool:
    del runtime_environment
    # Cancel orphaned asyncio tasks from previous tests (e.g. mock LLMs
    # with asyncio.sleep still running after worker stop).
    current = asyncio.current_task()
    for t in asyncio.all_tasks():
        if t is not current and not t.done() and "pytest" not in (t.get_name() or ""):
            t.cancel()
    await asyncio.sleep(0.2)
    await _force_clean()
    _wait_for_api(API_BASE, timeout_sec=30.0)
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=8)
    yield pool
    pool.terminate()


@pytest.fixture
def api_client(runtime_environment: RuntimeHandles) -> ApiClient:
    del runtime_environment
    return ApiClient(API_BASE)


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
            worker = self._workers.pop()
            await stop_worker(worker)


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
    """Unified context fixture used by all scenario tests."""
    return E2EContext(api=api_client, db=db, llm=llm_provider, workers=worker_manager)
