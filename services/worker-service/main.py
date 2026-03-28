"""Entry point for the worker service.

Usage:
    cd services/worker-service
    python main.py

Environment:
    DB_DSN  PostgreSQL connection string.
    Or split DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD values.
    TAVILY_API_KEY for web_search tool (optional)
"""

import asyncio
import logging
import os
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import quote, urlsplit, urlunsplit

from core.config import WorkerConfig
from core.db import create_pool
from core.worker import WorkerService
from executor.router import DefaultTaskRouter


def _log_runtime_env() -> None:
    """Log the runtime features available to the worker."""
    logger = logging.getLogger(__name__)

    if os.environ.get("TAVILY_API_KEY"):
        logger.info("TAVILY_API_KEY is set")
    else:
        logger.info("TAVILY_API_KEY is not set — web_search tool will be unavailable")


def _build_db_dsn() -> str:
    """Resolve the PostgreSQL connection string from either direct or split env vars."""
    dsn = os.environ.get("DB_DSN")
    if dsn:
        return dsn

    required_vars = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
    values = {name: os.environ.get(name) for name in required_vars}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "DB_DSN is not set and the following split DB env vars are missing: "
            f"{', '.join(missing)}"
        )

    netloc = (
        f"{quote(values['DB_USER'], safe='')}:{quote(values['DB_PASSWORD'], safe='')}@"
        f"{values['DB_HOST']}"
    )
    if values["DB_PORT"]:
        netloc = f"{netloc}:{values['DB_PORT']}"

    return urlunsplit(
        (
            "postgresql",
            netloc,
            f"/{quote(values['DB_NAME'], safe='')}",
            "",
            "",
        )
    )


def _format_db_endpoint(dsn: str) -> str:
    parsed = urlsplit(dsn)
    if parsed.scheme and parsed.hostname:
        port = f":{parsed.port}" if parsed.port else ""
        database = parsed.path.lstrip("/") or "<default>"
        return f"{parsed.scheme}://{parsed.hostname}{port}/{database}"
    return dsn


def _assert_langfuse_ready(config: WorkerConfig) -> None:
    if not config.langfuse_enabled or not config.langfuse_host:
        return

    try:
        with urlopen(config.langfuse_host, timeout=5) as response:
            if response.status >= 500:
                raise RuntimeError(
                    f"Unable to reach Langfuse at {config.langfuse_host} (status {response.status})"
                )
    except (URLError, OSError) as exc:
        raise RuntimeError(f"Unable to reach Langfuse at {config.langfuse_host}") from exc


async def main():
    # Configure stdlib logging so graph.py loggers are not silently swallowed
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    _log_runtime_env()

    try:
        dsn = _build_db_dsn()
    except RuntimeError as exc:
        logging.getLogger(__name__).error(str(exc))
        raise SystemExit(1) from exc

    logging.getLogger(__name__).info("Worker DB endpoint: %s", _format_db_endpoint(dsn))

    config = WorkerConfig(db_dsn=dsn)
    try:
        _assert_langfuse_ready(config)
    except RuntimeError as exc:
        logging.getLogger(__name__).error(str(exc))
        raise SystemExit(1) from exc

    pool = await create_pool(config.db_dsn)
    try:
        router = DefaultTaskRouter(config, pool)
        worker = WorkerService(config, pool, router)
        await worker.run_until_shutdown()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
