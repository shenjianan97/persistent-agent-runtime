"""Unit tests for ToolResultArtifactStore (Phase 2 Track 7 Follow-up, Task 4).

See docs/exec-plans/active/phase-2/track-7-follow-up/agent_tasks/task-4-tool-result-offload.md
for the contract. Covers:

- URI round-trip (result + arg variants)
- put / get round-trip byte-for-byte
- content-hash idempotency within the URI scheme
- missing-key returns None (S3 NoSuchKey only)
- transport / auth errors raise
- put does not mutate its input
- hash disambiguates same-tool_call_id retries
- parse_tool_result_uri rejects malformed input

Uses ``InMemoryToolResultStore`` for the core contract assertions and mocks
``boto3`` for the ``S3ToolResultStore`` variants.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from executor.compaction.tool_result_store import (
    InMemoryToolResultStore,
    S3ToolResultStore,
    ToolResultURI,
    parse_tool_result_uri,
)
from storage.s3_client import S3Client


# ---------------------------------------------------------------------------
# URI parse / build
# ---------------------------------------------------------------------------


class TestToolResultURIParse:
    def test_parse_result_uri_round_trip(self):
        uri = "toolresult://tenant-a/task-1/call-xyz/abcdef012345.txt"
        parsed = parse_tool_result_uri(uri)
        assert parsed.tenant_id == "tenant-a"
        assert parsed.task_id == "task-1"
        assert parsed.tool_call_id == "call-xyz"
        assert parsed.content_hash == "abcdef012345"
        assert parsed.arg_key is None

    def test_parse_arg_uri_round_trip(self):
        uri = "toolresult://tenant-a/task-1/call-xyz/args/content/abcdef012345.txt"
        parsed = parse_tool_result_uri(uri)
        assert parsed.tenant_id == "tenant-a"
        assert parsed.task_id == "task-1"
        assert parsed.tool_call_id == "call-xyz"
        assert parsed.arg_key == "content"
        assert parsed.content_hash == "abcdef012345"

    def test_parse_rejects_wrong_scheme(self):
        with pytest.raises(ValueError):
            parse_tool_result_uri("http://tenant/task/call/aaaaaaaaaaaa.txt")

    def test_parse_rejects_empty_components(self):
        with pytest.raises(ValueError):
            parse_tool_result_uri("toolresult:///task/call/aaaaaaaaaaaa.txt")

    def test_parse_rejects_missing_extension(self):
        with pytest.raises(ValueError):
            parse_tool_result_uri("toolresult://t/task/call/aaaaaaaaaaaa")

    def test_parse_rejects_bad_hash_shape(self):
        # content_hash must be 12 hex chars
        with pytest.raises(ValueError):
            parse_tool_result_uri("toolresult://t/task/call/ZZZZZZZZZZZZ.txt")
        with pytest.raises(ValueError):
            parse_tool_result_uri("toolresult://t/task/call/abc.txt")

    def test_parse_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_tool_result_uri("not a uri")
        with pytest.raises(ValueError):
            parse_tool_result_uri("")


# ---------------------------------------------------------------------------
# InMemoryToolResultStore contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestInMemoryToolResultStore:
    async def test_put_returns_result_uri(self):
        store = InMemoryToolResultStore()
        uri = await store.put(
            tenant_id="t1",
            task_id="task-1",
            tool_call_id="call-1",
            content="hello world",
        )
        assert uri.startswith("toolresult://t1/task-1/call-1/")
        assert uri.endswith(".txt")
        parsed = parse_tool_result_uri(uri)
        assert parsed.arg_key is None
        expected_hash = hashlib.sha256(b"hello world").hexdigest()[:12]
        assert parsed.content_hash == expected_hash

    async def test_put_returns_arg_uri(self):
        store = InMemoryToolResultStore()
        uri = await store.put(
            tenant_id="t1",
            task_id="task-1",
            tool_call_id="call-1",
            content="big content",
            arg_key="content",
        )
        assert "/args/content/" in uri
        parsed = parse_tool_result_uri(uri)
        assert parsed.arg_key == "content"

    async def test_get_round_trips(self):
        store = InMemoryToolResultStore()
        original = "some content " * 1000
        uri = await store.put(
            tenant_id="t1",
            task_id="task-1",
            tool_call_id="call-1",
            content=original,
        )
        got = await store.get(uri)
        assert got == original

    async def test_get_missing_returns_none(self):
        store = InMemoryToolResultStore()
        result = await store.get(
            "toolresult://t/task/call/aaaaaaaaaaaa.txt"
        )
        assert result is None

    async def test_put_does_not_mutate_input_string(self):
        store = InMemoryToolResultStore()
        original = "immutable content"
        # Strings are immutable in Python; assert value preserved.
        ref = original
        await store.put(
            tenant_id="t1",
            task_id="task-1",
            tool_call_id="call-1",
            content=original,
        )
        assert original == "immutable content"
        assert ref is original

    async def test_hash_disambiguates_retries(self):
        """Same (tenant, task, tool_call_id), different content → different URIs."""
        store = InMemoryToolResultStore()
        uri1 = await store.put(
            tenant_id="t1",
            task_id="task-1",
            tool_call_id="call-same",
            content="content one",
        )
        uri2 = await store.put(
            tenant_id="t1",
            task_id="task-1",
            tool_call_id="call-same",
            content="content TWO",
        )
        assert uri1 != uri2
        assert await store.get(uri1) == "content one"
        assert await store.get(uri2) == "content TWO"

    async def test_same_content_is_idempotent(self):
        store = InMemoryToolResultStore()
        uri1 = await store.put(
            tenant_id="t1",
            task_id="task-1",
            tool_call_id="call-same",
            content="same content",
        )
        uri2 = await store.put(
            tenant_id="t1",
            task_id="task-1",
            tool_call_id="call-same",
            content="same content",
        )
        assert uri1 == uri2


# ---------------------------------------------------------------------------
# S3ToolResultStore
# ---------------------------------------------------------------------------


def _make_s3_client() -> tuple[S3Client, MagicMock]:
    """Construct an S3Client with a mocked boto3 underneath."""
    with patch("storage.s3_client.boto3") as mock_boto3:
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3
        client = S3Client(
            endpoint_url="http://localhost:4566",
            bucket_name="platform-artifacts",
            region="us-east-1",
        )
    return client, mock_s3


@pytest.mark.asyncio
class TestS3ToolResultStore:
    async def test_put_uploads_under_uri_path_key(self):
        s3, mock = _make_s3_client()
        store = S3ToolResultStore(s3)
        uri = await store.put(
            tenant_id="t1",
            task_id="task-1",
            tool_call_id="call-1",
            content="hello",
        )
        # put_object called with Bucket=platform-artifacts and Key matching URI path.
        args, kwargs = mock.put_object.call_args
        assert kwargs["Bucket"] == "platform-artifacts"
        parsed = parse_tool_result_uri(uri)
        expected_key = (
            f"{parsed.tenant_id}/{parsed.task_id}/{parsed.tool_call_id}/"
            f"{parsed.content_hash}.txt"
        )
        assert kwargs["Key"] == expected_key
        assert kwargs["Body"] == b"hello"
        assert kwargs["ContentType"].startswith("text/plain")

    async def test_put_arg_key_goes_under_args_path(self):
        s3, mock = _make_s3_client()
        store = S3ToolResultStore(s3)
        uri = await store.put(
            tenant_id="t1",
            task_id="task-1",
            tool_call_id="call-1",
            content="hello",
            arg_key="new_string",
        )
        args, kwargs = mock.put_object.call_args
        parsed = parse_tool_result_uri(uri)
        expected_key = (
            f"{parsed.tenant_id}/{parsed.task_id}/{parsed.tool_call_id}/"
            f"args/new_string/{parsed.content_hash}.txt"
        )
        assert kwargs["Key"] == expected_key
        assert parsed.arg_key == "new_string"

    async def test_get_round_trips_bytes(self):
        s3, mock = _make_s3_client()
        body = MagicMock()
        body.read.return_value = b"round-trip"
        mock.get_object.return_value = {"Body": body}
        store = S3ToolResultStore(s3)
        out = await store.get(
            "toolresult://t/task/call/aaaaaaaaaaaa.txt"
        )
        assert out == "round-trip"

    async def test_get_no_such_key_returns_none(self):
        s3, mock = _make_s3_client()
        err = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
            "GetObject",
        )
        mock.get_object.side_effect = err
        store = S3ToolResultStore(s3)
        out = await store.get(
            "toolresult://t/task/call/aaaaaaaaaaaa.txt"
        )
        assert out is None

    async def test_get_404_returns_none(self):
        s3, mock = _make_s3_client()
        err = ClientError(
            {
                "Error": {"Code": "404", "Message": "Not Found"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "GetObject",
        )
        mock.get_object.side_effect = err
        store = S3ToolResultStore(s3)
        out = await store.get(
            "toolresult://t/task/call/aaaaaaaaaaaa.txt"
        )
        assert out is None

    async def test_get_transport_error_raises(self):
        """Non-NoSuchKey errors must propagate so Task 5's recall tool
        distinguishes them from retention GC."""
        s3, mock = _make_s3_client()
        err = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no perms"}},
            "GetObject",
        )
        mock.get_object.side_effect = err
        store = S3ToolResultStore(s3)
        with pytest.raises(ClientError):
            await store.get("toolresult://t/task/call/aaaaaaaaaaaa.txt")

    async def test_get_malformed_uri_raises_value_error(self):
        s3, _ = _make_s3_client()
        store = S3ToolResultStore(s3)
        with pytest.raises(ValueError):
            await store.get("not a uri")
