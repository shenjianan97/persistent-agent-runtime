"""Unit tests for the upload_artifact tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.upload_artifact import (
    MAX_CONTENT_BYTES,
    MAX_FILENAME_LENGTH,
    UploadArtifactArguments,
    UploadArtifactResult,
    execute_upload_artifact,
)


class TestUploadArtifactArguments:
    def test_valid_arguments(self):
        args = UploadArtifactArguments(
            filename="report.pdf",
            content="file content here",
            content_type="application/pdf",
        )
        assert args.filename == "report.pdf"
        assert args.content == "file content here"
        assert args.content_type == "application/pdf"

    def test_default_content_type(self):
        args = UploadArtifactArguments(
            filename="output.txt",
            content="some text",
        )
        assert args.content_type == "text/plain"

    def test_filename_too_long_rejected(self):
        with pytest.raises(Exception):
            UploadArtifactArguments(
                filename="x" * (MAX_FILENAME_LENGTH + 1),
                content="content",
            )

    def test_empty_filename_rejected(self):
        with pytest.raises(Exception):
            UploadArtifactArguments(
                filename="",
                content="content",
            )

    def test_empty_content_rejected(self):
        with pytest.raises(Exception):
            UploadArtifactArguments(
                filename="file.txt",
                content="",
            )


class TestUploadArtifactResult:
    def test_result_fields(self):
        result = UploadArtifactResult(
            filename="report.pdf",
            size_bytes=1024,
            content_type="application/pdf",
        )
        assert result.filename == "report.pdf"
        assert result.size_bytes == 1024
        assert result.content_type == "application/pdf"


class TestExecuteUploadArtifact:
    @pytest.mark.asyncio
    async def test_successful_upload(self):
        """upload_artifact should upload to S3 and insert DB row."""
        mock_s3 = MagicMock()
        mock_s3.upload = AsyncMock()
        mock_s3.build_key.return_value = "tenant-1/task-abc/output/report.txt"

        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await execute_upload_artifact(
            filename="report.txt",
            content="Hello, world!",
            content_type="text/plain",
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-abc",
            tenant_id="tenant-1",
        )

        assert result["filename"] == "report.txt"
        assert result["size_bytes"] == len("Hello, world!".encode("utf-8"))
        assert result["content_type"] == "text/plain"

        mock_s3.build_key.assert_called_once_with(
            tenant_id="tenant-1",
            task_id="task-abc",
            direction="output",
            filename="report.txt",
        )
        mock_s3.upload.assert_called_once_with(
            key="tenant-1/task-abc/output/report.txt",
            data=b"Hello, world!",
            content_type="text/plain",
        )

        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args
        assert "INSERT INTO task_artifacts" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_content_too_large_raises_error(self):
        """upload_artifact should reject content exceeding 50 MB."""
        mock_s3 = MagicMock()
        mock_s3.build_key.return_value = "key"
        mock_pool = MagicMock()

        large_content = "x" * (MAX_CONTENT_BYTES + 1)

        with pytest.raises(ValueError, match="Artifact content too large"):
            await execute_upload_artifact(
                filename="huge.txt",
                content=large_content,
                content_type="text/plain",
                s3_client=mock_s3,
                pool=mock_pool,
                task_id="task-abc",
                tenant_id="tenant-1",
            )

        mock_s3.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_upsert_on_duplicate_filename(self):
        """upload_artifact should use ON CONFLICT to handle duplicate filenames."""
        mock_s3 = MagicMock()
        mock_s3.upload = AsyncMock()
        mock_s3.build_key.return_value = "tenant-1/task-abc/output/data.csv"

        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        await execute_upload_artifact(
            filename="data.csv",
            content="col1,col2\n1,2",
            content_type="text/csv",
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-abc",
            tenant_id="tenant-1",
        )

        call_args = mock_conn.execute.call_args
        assert "ON CONFLICT" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_default_content_type(self):
        """upload_artifact should default to text/plain when content_type not specified."""
        mock_s3 = MagicMock()
        mock_s3.upload = AsyncMock()
        mock_s3.build_key.return_value = "t/task/output/file.txt"

        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await execute_upload_artifact(
            filename="file.txt",
            content="hello",
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task",
            tenant_id="t",
        )

        assert result["content_type"] == "text/plain"
        mock_s3.upload.assert_called_once_with(
            key="t/task/output/file.txt",
            data=b"hello",
            content_type="text/plain",
        )
