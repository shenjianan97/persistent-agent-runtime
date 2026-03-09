"""Entry point for the worker service.

Usage:
    cd services/worker-service
    python main.py

Environment:
    DB_DSN  PostgreSQL connection string (default: postgresql://localhost:55432/agent_runtime)
    ANTHROPIC_API_KEY / AWS credentials for LLM calls
    TAVILY_API_KEY for web_search tool
"""

import asyncio
import logging
import os

from core.config import WorkerConfig
from core.db import create_pool
from core.worker import WorkerService
from executor.router import DefaultTaskRouter


def _check_env():
    """Log which API keys are available at startup."""
    logger = logging.getLogger(__name__)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        masked = anthropic_key[:12] + "..." + anthropic_key[-4:]
        logger.info("ANTHROPIC_API_KEY is set (%s)", masked)
    else:
        logger.warning("ANTHROPIC_API_KEY is NOT set — Claude models will fail")

    if os.environ.get("TAVILY_API_KEY"):
        logger.info("TAVILY_API_KEY is set")
    else:
        logger.info("TAVILY_API_KEY is not set — web_search tool will be unavailable")


async def main():
    # Configure stdlib logging so graph.py loggers are not silently swallowed
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    _check_env()

    dsn = os.environ.get("DB_DSN")
    if not dsn:
        logging.getLogger(__name__).error(
            "DB_DSN environment variable is not set. "
            "Example: export DB_DSN=\"postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime\""
        )
        raise SystemExit(1)

    config = WorkerConfig(db_dsn=dsn)

    pool = await create_pool(config.db_dsn)
    try:
        router = DefaultTaskRouter(config, pool)
        worker = WorkerService(config, pool, router)
        await worker.run_until_shutdown()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
