import asyncio
import os
import sys

from core.config import WorkerConfig
from core.db import create_pool
from core.worker import WorkerService
from executor.router import DefaultTaskRouter

async def main():
    dsn = os.environ.get("DB_DSN", "postgresql://localhost:55432/agent_runtime")
    config = WorkerConfig(db_dsn=dsn)
    
    # Needs a real pool
    pool = await create_pool(config.db_dsn)
    
    router = DefaultTaskRouter(config, pool)
    worker = WorkerService(config, pool, router)
    
    try:
        await worker.run_until_shutdown()
    except KeyboardInterrupt:
        pass
    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
