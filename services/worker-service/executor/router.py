from typing import Protocol, Any
import asyncio

import asyncpg
from core.config import WorkerConfig


class TaskExecutor(Protocol):
    """Protocol defining how an executor behaves."""
    async def execute_task(self, task_data: dict[str, Any], cancel_event: asyncio.Event) -> None:
        """Execute a task and periodically check cancel_event.is_set() to abort early."""
        ...


class TaskRouter(Protocol):
    """Protocol defining how the runtime routes tasks to the correct executor."""
    def get_executor(self, task_data: dict[str, Any]) -> TaskExecutor:
        """Return the appropriate TaskExecutor based on task metadata."""
        ...


class DefaultTaskRouter:
    """Phase 1 Default Router. Always routes to the GraphExecutor."""
    
    def __init__(self, config: WorkerConfig, pool: asyncpg.Pool):
        self.config = config
        self.pool = pool
        
        # Instantiate executors
        from executor.graph import GraphExecutor
        self._graph_executor = GraphExecutor(config, pool)

    def get_executor(self, task_data: dict[str, Any]) -> TaskExecutor:
        # Future-proofing: In Phase 2, this could inspect task_data.get('worker_pool_id')
        # to route to a CustomToolRuntimeExecutor instead.
        return self._graph_executor
