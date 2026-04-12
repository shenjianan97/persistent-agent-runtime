"""upload_artifact built-in tool — allows agents to produce output files."""

from __future__ import annotations

from typing import Annotated

import asyncpg
import structlog
from pydantic import BaseModel, Field

from storage.s3_client import S3Client

logger = structlog.get_logger(__name__)

MAX_CONTENT_BYTES = 52_428_800  # 50 MB
MAX_FILENAME_LENGTH = 255
MAX_CONTENT_TYPE_LENGTH = 100


class UploadArtifactArguments(BaseModel):
    filename: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_FILENAME_LENGTH,
            description="Name for the output file (e.g., 'report.pdf', 'data.csv').",
        ),
    ]
    content: Annotated[
        str,
        Field(
            min_length=1,
            description="The file content as a string. For binary files, use base64 encoding.",
        ),
    ]
    content_type: Annotated[
        str,
        Field(
            max_length=MAX_CONTENT_TYPE_LENGTH,
            default="text/plain",
            description="MIME type of the content (e.g., 'text/plain', 'application/json', 'text/csv').",
        ),
    ]


class UploadArtifactResult(BaseModel):
    filename: str
    size_bytes: int
    content_type: str


async def execute_upload_artifact(
    *,
    filename: str,
    content: str,
    content_type: str = "text/plain",
    s3_client: S3Client,
    pool: asyncpg.Pool,
    task_id: str,
    tenant_id: str,
) -> dict:
    """Execute the upload_artifact tool."""
    # Encode content to bytes
    data = content.encode("utf-8")
    size_bytes = len(data)

    # Validate size
    if size_bytes > MAX_CONTENT_BYTES:
        raise ValueError(
            f"Artifact content too large: {size_bytes} bytes "
            f"(maximum {MAX_CONTENT_BYTES} bytes / 50 MB)"
        )

    # Build S3 key
    s3_key = s3_client.build_key(
        tenant_id=tenant_id,
        task_id=task_id,
        direction="output",
        filename=filename,
    )

    logger.info(
        "upload_artifact_started",
        task_id=task_id,
        tenant_id=tenant_id,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        s3_key=s3_key,
    )

    # Upload to S3
    await s3_client.upload(key=s3_key, data=data, content_type=content_type)

    # Insert artifact metadata into database
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO task_artifacts (task_id, tenant_id, filename, direction,
                                        content_type, size_bytes, s3_key)
            VALUES ($1, $2, $3, 'output', $4, $5, $6)
            ON CONFLICT (task_id, direction, filename)
            DO UPDATE SET content_type = EXCLUDED.content_type,
                          size_bytes = EXCLUDED.size_bytes,
                          s3_key = EXCLUDED.s3_key,
                          created_at = NOW()
            """,
            task_id,
            tenant_id,
            filename,
            content_type,
            size_bytes,
            s3_key,
        )

    logger.info(
        "upload_artifact_completed",
        task_id=task_id,
        tenant_id=tenant_id,
        filename=filename,
        size_bytes=size_bytes,
    )

    return {
        "filename": filename,
        "size_bytes": size_bytes,
        "content_type": content_type,
    }
