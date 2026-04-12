"""S3 client wrapper for artifact storage operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import boto3
import structlog

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client as Boto3S3Client

logger = structlog.get_logger(__name__)


class S3Client:
    """Async-compatible S3 client for artifact upload/download/delete.

    Wraps boto3 synchronous calls with asyncio.to_thread() for async compatibility.
    Works with both LocalStack (via endpoint_url) and real AWS (endpoint_url=None).
    """

    def __init__(
        self,
        endpoint_url: str | None,
        bucket_name: str,
        region: str = "us-east-1",
    ) -> None:
        self._bucket_name = bucket_name
        self._endpoint_url = endpoint_url
        kwargs: dict = {
            "region_name": region,
        }
        if endpoint_url is not None:
            kwargs["endpoint_url"] = endpoint_url
        self._client: Boto3S3Client = boto3.client("s3", **kwargs)
        logger.info(
            "s3_client_initialized",
            bucket=bucket_name,
            endpoint_url=endpoint_url or "default (AWS)",
            region=region,
        )

    @property
    def bucket_name(self) -> str:
        return self._bucket_name

    async def upload(self, key: str, data: bytes, content_type: str) -> None:
        """Upload data to S3."""
        logger.info(
            "s3_upload_started",
            bucket=self._bucket_name,
            key=key,
            size_bytes=len(data),
            content_type=content_type,
        )
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.info(
            "s3_upload_completed",
            bucket=self._bucket_name,
            key=key,
            size_bytes=len(data),
        )

    async def download(self, key: str) -> bytes:
        """Download data from S3."""
        logger.info(
            "s3_download_started",
            bucket=self._bucket_name,
            key=key,
        )
        response = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self._bucket_name,
            Key=key,
        )
        data = response["Body"].read()
        logger.info(
            "s3_download_completed",
            bucket=self._bucket_name,
            key=key,
            size_bytes=len(data),
        )
        return data

    async def delete(self, key: str) -> None:
        """Delete an object from S3."""
        logger.info(
            "s3_delete_started",
            bucket=self._bucket_name,
            key=key,
        )
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket_name,
            Key=key,
        )
        logger.info(
            "s3_delete_completed",
            bucket=self._bucket_name,
            key=key,
        )

    def build_key(
        self, tenant_id: str, task_id: str, direction: str, filename: str
    ) -> str:
        """Build an S3 object key from artifact metadata.

        Returns:
            S3 key in format: {tenant_id}/{task_id}/{direction}/{filename}
        """
        return f"{tenant_id}/{task_id}/{direction}/{filename}"
