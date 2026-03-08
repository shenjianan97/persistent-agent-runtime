import json
import uuid
from typing import Any

import asyncpg


class DbHelper:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def clean(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM checkpoint_writes")
            await conn.execute("DELETE FROM checkpoints")
            await conn.execute("DELETE FROM tasks")

    async def fetch_task(self, task_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1::uuid", task_id)
            return dict(row) if row else None

    async def fetch_task_columns(self, task_id: str, *columns: str) -> dict[str, Any] | None:
        cols = ", ".join(columns)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT {cols} FROM tasks WHERE task_id = $1::uuid", task_id)
            return dict(row) if row else None

    async def fetch_checkpoints(self, task_id: str) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT checkpoint_id, worker_id, checkpoint_payload, metadata_payload, cost_microdollars, created_at
                FROM checkpoints
                WHERE task_id = $1::uuid AND checkpoint_ns = ''
                ORDER BY created_at ASC
                """,
                task_id,
            )
            return [dict(row) for row in rows]

    async def checkpoint_count(self, task_id: str) -> int:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COUNT(*) FROM checkpoints WHERE task_id = $1::uuid AND checkpoint_ns = ''",
                task_id,
            )
            return int(value or 0)

    async def execute(self, sql: str, *args: Any) -> str:
        async with self.pool.acquire() as conn:
            return await conn.execute(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def notify_new_task(self, pool_id: str = "shared") -> None:
        await self.execute("SELECT pg_notify('new_task', $1)", pool_id)

    async def set_task_timeout(self, task_id: str, timeout_seconds: int) -> None:
        await self.execute(
            "UPDATE tasks SET task_timeout_seconds = $1, updated_at = NOW() WHERE task_id = $2::uuid",
            timeout_seconds,
            task_id,
        )

    async def expire_lease(self, task_id: str, *, worker_id: str | None = None) -> None:
        if worker_id:
            await self.execute(
                """
                UPDATE tasks
                SET status='running', lease_owner=$1, lease_expiry=NOW() - INTERVAL '1 second', updated_at=NOW()
                WHERE task_id=$2::uuid
                """,
                worker_id,
                task_id,
            )
        else:
            await self.execute(
                "UPDATE tasks SET lease_expiry=NOW() - INTERVAL '1 second', updated_at=NOW() WHERE task_id=$1::uuid",
                task_id,
            )

    async def set_retry_after_future(self, task_id: str, seconds: int) -> None:
        await self.execute(
            """
            UPDATE tasks
            SET status='queued', retry_after=NOW() + ($1 * INTERVAL '1 second'), lease_owner=NULL, lease_expiry=NULL, updated_at=NOW()
            WHERE task_id=$2::uuid
            """,
            seconds,
            task_id,
        )

    async def set_retry_after_past(self, task_id: str) -> None:
        await self.execute(
            "UPDATE tasks SET retry_after=NOW() - INTERVAL '1 second', updated_at=NOW() WHERE task_id=$1::uuid",
            task_id,
        )

    async def insert_task(
        self,
        *,
        tenant_id: str = "default",
        agent_id: str = "e2e_agent",
        status: str = "queued",
        input_text: str = "test",
        max_retries: int = 3,
        retry_count: int = 0,
        max_steps: int = 10,
        timeout_seconds: int = 300,
        worker_pool_id: str = "shared",
        lease_owner: str | None = None,
        lease_expiry_sql: str | None = None,
        retry_after_sql: str | None = None,
        dead_letter_reason: str | None = None,
        created_at_sql: str | None = None,
    ) -> str:
        task_id = str(uuid.uuid4())
        agent_config = {
            "system_prompt": "You are a test assistant.",
            "model": "claude-sonnet-4-6",
            "temperature": 0.5,
            "allowed_tools": ["calculator"],
        }
        lease_expiry_expr = lease_expiry_sql or "NULL"
        retry_after_expr = retry_after_sql or "NULL"
        created_at_expr = created_at_sql or "NOW()"

        sql = f"""
            INSERT INTO tasks (
                task_id, tenant_id, agent_id, agent_config_snapshot, worker_pool_id,
                status, input, max_retries, retry_count, max_steps, task_timeout_seconds,
                lease_owner, lease_expiry, retry_after, dead_letter_reason, created_at, updated_at
            ) VALUES (
                $1::uuid, $2, $3, $4::jsonb, $5,
                $6, $7, $8, $9, $10, $11,
                $12, {lease_expiry_expr}, {retry_after_expr}, $13, {created_at_expr}, NOW()
            )
        """

        await self.execute(
            sql,
            task_id,
            tenant_id,
            agent_id,
            json.dumps(agent_config),
            worker_pool_id,
            status,
            input_text,
            max_retries,
            retry_count,
            max_steps,
            timeout_seconds,
            lease_owner,
            dead_letter_reason,
        )
        return task_id

    async def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        lease_owner: str | None = None,
        lease_expiry_sql: str | None = None,
        retry_count: int | None = None,
        max_retries: int | None = None,
        retry_after_sql: str | None = None,
        dead_letter_reason: str | None = None,
        last_error_code: str | None = None,
        last_error_message: str | None = None,
    ) -> None:
        parts: list[str] = []
        args: list[Any] = []

        if status is not None:
            args.append(status)
            parts.append(f"status = ${len(args)}")
        if lease_owner is not None:
            args.append(lease_owner)
            parts.append(f"lease_owner = ${len(args)}")
        if lease_expiry_sql is not None:
            parts.append(f"lease_expiry = {lease_expiry_sql}")
        if retry_count is not None:
            args.append(retry_count)
            parts.append(f"retry_count = ${len(args)}")
        if max_retries is not None:
            args.append(max_retries)
            parts.append(f"max_retries = ${len(args)}")
        if retry_after_sql is not None:
            parts.append(f"retry_after = {retry_after_sql}")
        if dead_letter_reason is not None:
            args.append(dead_letter_reason)
            parts.append(f"dead_letter_reason = ${len(args)}")
        if last_error_code is not None:
            args.append(last_error_code)
            parts.append(f"last_error_code = ${len(args)}")
        if last_error_message is not None:
            args.append(last_error_message)
            parts.append(f"last_error_message = ${len(args)}")

        if not parts:
            return

        args.append(task_id)
        parts.append("updated_at = NOW()")
        sql = f"UPDATE tasks SET {', '.join(parts)} WHERE task_id = ${len(args)}::uuid"
        await self.execute(sql, *args)
