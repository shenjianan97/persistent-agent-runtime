"""Built-in sandbox tools for E2B code execution environments.

Tools in this module are conditionally registered when the agent has
sandbox.enabled: true. They receive the sandbox instance via closure.
"""

import asyncio
import logging
import mimetypes
import time
from typing import Annotated

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_COMMAND_TIMEOUT_SECONDS = 300


# --- sandbox_exec ---

class SandboxExecArguments(BaseModel):
    command: Annotated[
        str,
        Field(
            min_length=1,
            max_length=10000,
            description="Shell command to execute in the sandbox.",
        ),
    ]


class SandboxExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


def create_sandbox_exec_fn(sandbox, *, command_timeout: int = DEFAULT_COMMAND_TIMEOUT_SECONDS):
    """Create the sandbox_exec async function with the sandbox bound via closure.

    Args:
        sandbox: E2B Sandbox instance
        command_timeout: Maximum seconds per command execution

    Returns:
        Async function suitable for StructuredTool.from_function(coroutine=...)
    """

    async def sandbox_exec(command: str) -> dict:
        start_time = time.monotonic()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(sandbox.commands.run, command),
                timeout=command_timeout,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_exec_completed",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "command_length": len(command),
                    "exit_code": result.exit_code,
                    "stdout_length": len(result.stdout) if result.stdout else 0,
                    "stderr_length": len(result.stderr) if result.stderr else 0,
                    "duration_ms": duration_ms,
                },
            )

            return {
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "exit_code": result.exit_code,
            }

        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning(
                "sandbox_exec_timeout",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "command_length": len(command),
                    "timeout_seconds": command_timeout,
                    "duration_ms": duration_ms,
                },
            )
            return {
                "stdout": "",
                "stderr": f"Command timed out after {command_timeout} seconds",
                "exit_code": -1,
            }

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "sandbox_exec_error",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "command_length": len(command),
                    "error": str(e),
                    "duration_ms": duration_ms,
                },
            )
            return {
                "stdout": "",
                "stderr": f"Command execution failed: {str(e)}",
                "exit_code": -1,
            }

    return sandbox_exec


# --- sandbox_read_file ---

class SandboxReadFileArguments(BaseModel):
    path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1000,
            description="Absolute or relative path of the file to read in the sandbox.",
        ),
    ]


class SandboxReadFileResult(BaseModel):
    path: str
    content: str


def create_sandbox_read_file_fn(sandbox):
    """Create the sandbox_read_file async function with the sandbox bound via closure.

    Args:
        sandbox: E2B Sandbox instance

    Returns:
        Async function suitable for StructuredTool.from_function(coroutine=...)
    """

    async def sandbox_read_file(path: str) -> dict:
        start_time = time.monotonic()
        try:
            content = await asyncio.to_thread(sandbox.files.read, path)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # E2B files.read() returns bytes for binary files, str for text
            if isinstance(content, bytes):
                try:
                    content = content.decode("utf-8")
                except UnicodeDecodeError:
                    content = f"[Binary file: {len(content)} bytes. Use sandbox_download to retrieve binary files.]"

            logger.info(
                "sandbox_read_file_completed",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "path": path,
                    "content_length": len(content),
                    "duration_ms": duration_ms,
                },
            )

            return {
                "path": path,
                "content": content,
            }

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "sandbox_read_file_error",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "path": path,
                    "error": str(e),
                    "duration_ms": duration_ms,
                },
            )
            return {
                "path": path,
                "content": f"Error reading file: {str(e)}",
            }

    return sandbox_read_file


# --- sandbox_write_file ---

class SandboxWriteFileArguments(BaseModel):
    path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1000,
            description="Absolute or relative path where the file will be written in the sandbox.",
        ),
    ]
    content: Annotated[
        str,
        Field(
            max_length=52428800,
            description="Content to write to the file.",
        ),
    ]


class SandboxWriteFileResult(BaseModel):
    path: str
    size_bytes: int


def create_sandbox_write_file_fn(sandbox):
    """Create the sandbox_write_file async function with the sandbox bound via closure.

    Args:
        sandbox: E2B Sandbox instance

    Returns:
        Async function suitable for StructuredTool.from_function(coroutine=...)
    """

    async def sandbox_write_file(path: str, content: str) -> dict:
        start_time = time.monotonic()
        try:
            await asyncio.to_thread(sandbox.files.write, path, content)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            size_bytes = len(content.encode("utf-8"))

            logger.info(
                "sandbox_write_file_completed",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "path": path,
                    "size_bytes": size_bytes,
                    "duration_ms": duration_ms,
                },
            )

            return {
                "path": path,
                "size_bytes": size_bytes,
            }

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "sandbox_write_file_error",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "path": path,
                    "error": str(e),
                    "duration_ms": duration_ms,
                },
            )
            return {
                "path": path,
                "size_bytes": 0,
            }

    return sandbox_write_file


# --- sandbox_download ---

class SandboxDownloadArguments(BaseModel):
    path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1000,
            description="Path in the sandbox filesystem to download as an output artifact.",
        ),
    ]
    filename: Annotated[
        str | None,
        Field(
            default=None,
            max_length=255,
            description="Output artifact filename. Defaults to the basename of the path.",
        ),
    ] = None


class SandboxDownloadResult(BaseModel):
    filename: str
    size_bytes: int
    content_type: str


def create_sandbox_download_fn(sandbox, *, s3_client, pool, task_id: str, tenant_id: str):
    """Create the sandbox_download async function with dependencies bound via closure.

    Args:
        sandbox: E2B Sandbox instance
        s3_client: Track 1 S3Client instance for uploading to artifact storage
        pool: asyncpg connection pool for inserting task_artifacts rows
        task_id: UUID string of the current task
        tenant_id: tenant ID for S3 key construction

    Returns:
        Async function suitable for StructuredTool.from_function(coroutine=...)
    """
    import os

    async def sandbox_download(path: str, filename: str | None = None) -> dict:
        start_time = time.monotonic()
        try:
            # 1. Read file from sandbox
            data = await asyncio.to_thread(sandbox.files.read, path)

            # Ensure we have bytes
            if isinstance(data, str):
                data = data.encode("utf-8")

            size_bytes = len(data)

            # 2. Determine filename
            effective_filename = filename or os.path.basename(path)
            if not effective_filename:
                effective_filename = "download"

            # 3. Infer content type from file extension
            content_type, _ = mimetypes.guess_type(effective_filename)
            if content_type is None:
                content_type = "application/octet-stream"

            # 4. Upload to S3 via Track 1's S3Client
            s3_key = s3_client.build_key(tenant_id, task_id, "output", effective_filename)
            await s3_client.upload(s3_key, data, content_type)

            # 5. Insert task_artifacts row
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO task_artifacts (task_id, tenant_id, filename, direction, content_type, size_bytes, s3_key)
                       VALUES ($1::uuid, $2, $3, 'output', $4, $5, $6)
                       ON CONFLICT (task_id, direction, filename) DO UPDATE
                       SET content_type = EXCLUDED.content_type,
                           size_bytes = EXCLUDED.size_bytes,
                           s3_key = EXCLUDED.s3_key""",
                    task_id,
                    tenant_id,
                    effective_filename,
                    content_type,
                    size_bytes,
                    s3_key,
                )

            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_download_completed",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "task_id": task_id,
                    "path": path,
                    "filename": effective_filename,
                    "size_bytes": size_bytes,
                    "content_type": content_type,
                    "duration_ms": duration_ms,
                },
            )

            return {
                "filename": effective_filename,
                "size_bytes": size_bytes,
                "content_type": content_type,
            }

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "sandbox_download_error",
                extra={
                    "sandbox_id": sandbox.sandbox_id,
                    "task_id": task_id,
                    "path": path,
                    "error": str(e),
                    "duration_ms": duration_ms,
                },
            )
            return {
                "filename": filename or os.path.basename(path) or "download",
                "size_bytes": 0,
                "content_type": "application/octet-stream",
            }

    return sandbox_download
