<!-- AGENT_TASK_START: task-3-worker-s3-client.md -->

# Task 3 — Worker S3 Client

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 2: Artifact Storage)
2. `docs/exec-plans/active/agent-capabilities/track-1/plan.md` — Track 1 execution plan
3. `services/worker-service/tools/definitions.py` — existing tool patterns and dependencies
4. `services/worker-service/pyproject.toml` — current dependencies

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-1/progress.md` to "Done".

## Context

Track 1 needs the worker to upload artifacts to S3. This task creates a Python S3 client wrapper around `boto3` that handles upload, download, and delete operations. It uses `asyncio.to_thread()` to wrap boto3's synchronous calls for async compatibility.

The client works with both LocalStack (`http://localhost:4566`) and real AWS. When `endpoint_url` is `None`, boto3 uses standard AWS endpoint resolution.

## Task-Specific Shared Contract

- S3 key format: `{tenant_id}/{task_id}/{direction}/{filename}`
- Bucket name: configurable, defaults to `platform-artifacts`
- Endpoint URL: configurable via constructor, `None` for real AWS
- All S3 operations must be wrapped in `asyncio.to_thread()` since boto3 is synchronous
- Use `structlog` for logging, consistent with the rest of the worker service

## Affected Component

- **Service/Module:** Worker Service — Storage
- **File paths:**
  - `services/worker-service/storage/__init__.py` (new — empty package init)
  - `services/worker-service/storage/s3_client.py` (new — S3 client class)
  - `services/worker-service/pyproject.toml` (modify — add `boto3` dependency)
  - `services/worker-service/tests/storage/__init__.py` (new — empty test package init)
  - `services/worker-service/tests/storage/test_s3_client.py` (new — unit tests)
- **Change type:** new code + dependency addition

## Dependencies

- **Must complete first:** Task 1 (DB Migration — for schema context), Task 2 (LocalStack Setup — for S3 endpoint)
- **Provides output to:** Task 6 (upload_artifact Tool — uses S3Client for uploads)
- **Shared interfaces/contracts:** `S3Client` class API consumed by Task 6

## Implementation Specification

### Step 1: Create storage package init

Create `services/worker-service/storage/__init__.py`:

```python
```

(Empty file — package init only.)

### Step 2: Create S3 client module

Create `services/worker-service/storage/s3_client.py`:

```python
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
        """Upload data to S3.

        Args:
            key: S3 object key
            data: File content as bytes
            content_type: MIME type of the content
        """
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
        """Download data from S3.

        Args:
            key: S3 object key

        Returns:
            File content as bytes
        """
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
        """Delete an object from S3.

        Args:
            key: S3 object key
        """
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

        Args:
            tenant_id: Tenant identifier
            task_id: Task identifier
            direction: 'input' or 'output'
            filename: Original filename

        Returns:
            S3 key in format: {tenant_id}/{task_id}/{direction}/{filename}
        """
        return f"{tenant_id}/{task_id}/{direction}/{filename}"
```

### Step 3: Add boto3 dependency to pyproject.toml

Add `boto3` to the `dependencies` list in `services/worker-service/pyproject.toml`:

```toml
"boto3>=1.35.0",
```

Add it after the existing `beautifulsoup4` entry in the dependencies list.

Then run dependency installation:
```bash
cd services/worker-service && uv pip install -e ".[dev]"
```

### Step 4: Create test package init

Create `services/worker-service/tests/storage/__init__.py`:

```python
```

(Empty file — test package init only.)

### Step 5: Create unit tests

Create `services/worker-service/tests/storage/test_s3_client.py`:

```python
"""Unit tests for the S3 client wrapper."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from storage.s3_client import S3Client


class TestS3ClientInit:
    def test_creates_client_with_endpoint_url(self):
        """S3Client should pass endpoint_url to boto3 when provided."""
        with patch("storage.s3_client.boto3") as mock_boto3:
            client = S3Client(
                endpoint_url="http://localhost:4566",
                bucket_name="test-bucket",
                region="us-east-1",
            )
            mock_boto3.client.assert_called_once_with(
                "s3",
                region_name="us-east-1",
                endpoint_url="http://localhost:4566",
            )
            assert client.bucket_name == "test-bucket"

    def test_creates_client_without_endpoint_url(self):
        """S3Client should not pass endpoint_url to boto3 when None."""
        with patch("storage.s3_client.boto3") as mock_boto3:
            client = S3Client(
                endpoint_url=None,
                bucket_name="prod-bucket",
                region="us-west-2",
            )
            mock_boto3.client.assert_called_once_with(
                "s3",
                region_name="us-west-2",
            )
            assert client.bucket_name == "prod-bucket"


class TestS3ClientUpload:
    @pytest.mark.asyncio
    async def test_upload_calls_put_object(self):
        """upload() should call put_object with correct parameters."""
        with patch("storage.s3_client.boto3") as mock_boto3:
            mock_s3 = MagicMock()
            mock_boto3.client.return_value = mock_s3

            client = S3Client(
                endpoint_url="http://localhost:4566",
                bucket_name="test-bucket",
            )

            await client.upload(
                key="tenant/task/output/report.pdf",
                data=b"file content",
                content_type="application/pdf",
            )

            mock_s3.put_object.assert_called_once_with(
                Bucket="test-bucket",
                Key="tenant/task/output/report.pdf",
                Body=b"file content",
                ContentType="application/pdf",
            )


class TestS3ClientDownload:
    @pytest.mark.asyncio
    async def test_download_calls_get_object(self):
        """download() should call get_object and return body bytes."""
        with patch("storage.s3_client.boto3") as mock_boto3:
            mock_s3 = MagicMock()
            mock_body = MagicMock()
            mock_body.read.return_value = b"downloaded content"
            mock_s3.get_object.return_value = {"Body": mock_body}
            mock_boto3.client.return_value = mock_s3

            client = S3Client(
                endpoint_url="http://localhost:4566",
                bucket_name="test-bucket",
            )

            result = await client.download(key="tenant/task/output/report.pdf")

            mock_s3.get_object.assert_called_once_with(
                Bucket="test-bucket",
                Key="tenant/task/output/report.pdf",
            )
            assert result == b"downloaded content"


class TestS3ClientDelete:
    @pytest.mark.asyncio
    async def test_delete_calls_delete_object(self):
        """delete() should call delete_object with correct parameters."""
        with patch("storage.s3_client.boto3") as mock_boto3:
            mock_s3 = MagicMock()
            mock_boto3.client.return_value = mock_s3

            client = S3Client(
                endpoint_url="http://localhost:4566",
                bucket_name="test-bucket",
            )

            await client.delete(key="tenant/task/output/report.pdf")

            mock_s3.delete_object.assert_called_once_with(
                Bucket="test-bucket",
                Key="tenant/task/output/report.pdf",
            )


class TestS3ClientBuildKey:
    def test_build_key_format(self):
        """build_key() should produce {tenant}/{task}/{direction}/{filename}."""
        with patch("storage.s3_client.boto3"):
            client = S3Client(
                endpoint_url="http://localhost:4566",
                bucket_name="test-bucket",
            )

            key = client.build_key(
                tenant_id="tenant-1",
                task_id="task-abc",
                direction="output",
                filename="report.pdf",
            )

            assert key == "tenant-1/task-abc/output/report.pdf"

    def test_build_key_input_direction(self):
        """build_key() should work with 'input' direction."""
        with patch("storage.s3_client.boto3"):
            client = S3Client(
                endpoint_url=None,
                bucket_name="prod-bucket",
            )

            key = client.build_key(
                tenant_id="acme",
                task_id="12345",
                direction="input",
                filename="invoice.pdf",
            )

            assert key == "acme/12345/input/invoice.pdf"
```

### Step 6: Update pyproject.toml packages

Update the `[tool.setuptools.packages.find]` section to include the new `storage` package:

```toml
[tool.setuptools.packages.find]
where = ["."]
include = ["core*", "checkpointer*", "tools*", "storage*"]
```

## Acceptance Criteria

- [ ] `services/worker-service/storage/__init__.py` exists (empty package init)
- [ ] `services/worker-service/storage/s3_client.py` exists with `S3Client` class
- [ ] `S3Client.__init__()` accepts `endpoint_url`, `bucket_name`, `region` and creates a boto3 client
- [ ] `S3Client.upload()` calls `put_object` via `asyncio.to_thread()`
- [ ] `S3Client.download()` calls `get_object` via `asyncio.to_thread()` and returns bytes
- [ ] `S3Client.delete()` calls `delete_object` via `asyncio.to_thread()`
- [ ] `S3Client.build_key()` returns `{tenant_id}/{task_id}/{direction}/{filename}`
- [ ] `boto3>=1.35.0` is in `pyproject.toml` dependencies
- [ ] `storage*` is in `pyproject.toml` packages include list
- [ ] All unit tests in `tests/storage/test_s3_client.py` pass
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests:** Test client initialization with and without `endpoint_url`. Test `upload()` calls `put_object` with correct args. Test `download()` calls `get_object` and returns body bytes. Test `delete()` calls `delete_object`. Test `build_key()` produces correct format for both `input` and `output` directions.
- **Regression tests:** Run `make test` — all existing worker tests must still pass.

## Constraints and Guardrails

- Do not create an abstract storage interface — `S3Client` is the concrete implementation.
- Do not add retry logic to S3 operations — boto3 handles retries internally.
- Do not add file size validation — that is handled by the `upload_artifact` tool (Task 6).
- Do not implement multipart upload — files are limited to 50 MB which is within S3's single PUT limit.
- Use `asyncio.to_thread()` for all boto3 calls — do not use `aioboto3` or other async S3 libraries.
- Use `structlog` for logging, not the standard `logging` module.

## Assumptions

- `boto3` is available after adding it to `pyproject.toml` and running `pip install`.
- The `TYPE_CHECKING` import for `mypy_boto3_s3` is optional — it provides type hints but is not a runtime dependency.
- The S3 client is instantiated once per worker process and shared across tasks.
- LocalStack is running on `http://localhost:4566` during local development (from Task 2).

<!-- AGENT_TASK_END: task-3-worker-s3-client.md -->
