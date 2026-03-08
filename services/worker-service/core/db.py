"""Database connection management using asyncpg."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    pass


async def create_pool(dsn: str, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    """Create an asyncpg connection pool.

    Args:
        dsn: PostgreSQL connection string.
        min_size: Minimum number of connections in the pool.
        max_size: Maximum number of connections in the pool.

    Returns:
        An asyncpg connection pool.
    """
    return await asyncpg.create_pool(dsn=dsn, min_size=min_size, max_size=max_size)


async def create_listen_connection(dsn: str) -> asyncpg.Connection:
    """Create a dedicated connection for LISTEN/NOTIFY.

    A separate connection is used because LISTEN blocks the connection
    and cannot be mixed with transactional queries.

    Args:
        dsn: PostgreSQL connection string.

    Returns:
        An asyncpg connection configured for LISTEN.
    """
    return await asyncpg.connect(dsn=dsn)
