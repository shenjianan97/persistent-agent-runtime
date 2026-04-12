"""Unit tests for the S3 client wrapper."""

from __future__ import annotations

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
