"""Tier 0 ingestion-offload artifact store (Phase 2 Track 7 Follow-up, Task 4).

See docs/exec-plans/active/phase-2/track-7-follow-up/agent_tasks/task-4-tool-result-offload.md
for the full contract.

Large tool-result content AND large string-valued keys inside
``AIMessage.tool_calls[*].args`` (`content`, `new_string`, `old_string`,
`text`, `body`) are persisted to S3 at ingestion time and replaced in state
with a URI + preview placeholder. This module owns the URI scheme, the
abstract store interface, an S3-backed production implementation, and an
in-memory test double.

URI scheme
----------

- Tool-RESULT:  ``toolresult://{tenant_id}/{task_id}/{tool_call_id}/{content_hash}.txt``
- Tool-ARG:     ``toolresult://{tenant_id}/{task_id}/{tool_call_id}/args/{arg_key}/{content_hash}.txt``

``content_hash`` is the first 12 hex characters of
``sha256(content.encode("utf-8"))``. Including the hash in the key is
load-bearing: ``tool_call_id`` is NOT guaranteed unique across provider-level
retries (Bedrock can reuse a ``tooluse_*`` id with different content), so a
per-content hash ensures each offload lands at its own S3 key and replays
are safe.

Non-mutation contract
---------------------

``put(content=...)`` MUST NOT mutate its ``content`` argument, any caller-
held reference, or any LangGraph state. It reads, hashes, writes, returns
the URI. Nothing else.

Error semantics
---------------

``get`` returns ``None`` **only** when the underlying key is missing
(``NoSuchKey`` / HTTP 404). Transport, auth, and any other backend error
MUST propagate — Task 5's recall tool distinguishes these from retention GC.
"""

from __future__ import annotations

import abc
import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from storage.s3_client import S3Client


URI_SCHEME: str = "toolresult://"
CONTENT_HASH_LEN: int = 12
# 12 hex chars, strictly.
_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{12}$")
_ARGS_SEGMENT = "args"
_CONTENT_TYPE = "text/plain; charset=utf-8"


@dataclass(frozen=True)
class ToolResultURI:
    """Parsed components of a ``toolresult://`` URI.

    Attributes:
        tenant_id: Tenant that owns the artifact.
        task_id: Task the artifact belongs to.
        tool_call_id: Tool call whose result / arg this artifact captures.
        content_hash: First 12 hex chars of ``sha256(content_bytes)``.
        arg_key: When present, identifies which ``AIMessage.tool_calls[*].args``
            key this artifact captured. ``None`` for tool RESULTs.
    """

    tenant_id: str
    task_id: str
    tool_call_id: str
    content_hash: str
    arg_key: str | None = None

    def to_key(self) -> str:
        """Return the S3 object key corresponding to this URI."""
        base = f"{self.tenant_id}/{self.task_id}/{self.tool_call_id}"
        if self.arg_key is None:
            return f"{base}/{self.content_hash}.txt"
        return f"{base}/{_ARGS_SEGMENT}/{self.arg_key}/{self.content_hash}.txt"

    def to_uri(self) -> str:
        """Return the canonical ``toolresult://`` URI string."""
        return f"{URI_SCHEME}{self.to_key()}"


def _hash_content(content: str) -> str:
    """Return the first ``CONTENT_HASH_LEN`` hex chars of sha256(content)."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return digest[:CONTENT_HASH_LEN]


def _build_uri(
    *,
    tenant_id: str,
    task_id: str,
    tool_call_id: str,
    content: str,
    arg_key: str | None = None,
) -> ToolResultURI:
    """Construct a ``ToolResultURI`` for ``content`` (does NOT touch S3)."""
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    if not task_id:
        raise ValueError("task_id must be non-empty")
    if not tool_call_id:
        raise ValueError("tool_call_id must be non-empty")
    if arg_key is not None and not arg_key:
        raise ValueError("arg_key, when provided, must be non-empty")
    return ToolResultURI(
        tenant_id=tenant_id,
        task_id=task_id,
        tool_call_id=tool_call_id,
        content_hash=_hash_content(content),
        arg_key=arg_key,
    )


def parse_tool_result_uri(s: str) -> ToolResultURI:
    """Parse a ``toolresult://`` URI. Raises ``ValueError`` on malformed input.

    Agent-supplied URIs (Task 5's recall tool) are untrusted — this parser
    is the gate that validates shape before any S3 lookup is attempted.

    Accepted forms:
        - ``toolresult://{tenant_id}/{task_id}/{tool_call_id}/{hash}.txt``
        - ``toolresult://{tenant_id}/{task_id}/{tool_call_id}/args/{arg_key}/{hash}.txt``

    Rejected:
        - Wrong / missing scheme
        - Empty ``tenant_id`` / ``task_id`` / ``tool_call_id`` / ``arg_key``
        - Missing ``.txt`` extension
        - Content-hash not exactly 12 lowercase hex chars
    """
    if not isinstance(s, str) or not s.startswith(URI_SCHEME):
        raise ValueError(f"not a toolresult URI: {s!r}")

    path = s[len(URI_SCHEME):]
    parts = path.split("/")

    # Result form: 4 parts  → tenant / task / call / hash.txt
    # Arg form:    6 parts  → tenant / task / call / args / arg_key / hash.txt
    if len(parts) not in (4, 6):
        raise ValueError(f"malformed toolresult URI (wrong segment count): {s!r}")

    tenant_id, task_id, tool_call_id = parts[0], parts[1], parts[2]
    if not tenant_id or not task_id or not tool_call_id:
        raise ValueError(f"malformed toolresult URI (empty component): {s!r}")

    arg_key: str | None
    filename: str
    if len(parts) == 4:
        arg_key = None
        filename = parts[3]
    else:
        if parts[3] != _ARGS_SEGMENT:
            raise ValueError(
                f"malformed toolresult URI (expected 'args' segment): {s!r}"
            )
        arg_key = parts[4]
        filename = parts[5]
        if not arg_key:
            raise ValueError(f"malformed toolresult URI (empty arg_key): {s!r}")

    if not filename.endswith(".txt"):
        raise ValueError(f"malformed toolresult URI (expected .txt): {s!r}")
    content_hash = filename[: -len(".txt")]
    if not _CONTENT_HASH_RE.match(content_hash):
        raise ValueError(
            f"malformed toolresult URI (bad content hash): {s!r}"
        )

    return ToolResultURI(
        tenant_id=tenant_id,
        task_id=task_id,
        tool_call_id=tool_call_id,
        content_hash=content_hash,
        arg_key=arg_key,
    )


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class ToolResultArtifactStore(abc.ABC):
    """Abstract store for Tier 0 ingestion-offloaded content.

    See module docstring for URI scheme, non-mutation contract, and error
    semantics. Implementations MUST honour both.
    """

    @abc.abstractmethod
    async def put(
        self,
        *,
        tenant_id: str,
        task_id: str,
        tool_call_id: str,
        content: str,
        arg_key: str | None = None,
    ) -> str:
        """Persist ``content`` and return the URI under which it can be recalled.

        ``arg_key``:
            - ``None`` → tool-RESULT URI.
            - non-empty str → tool-ARG URI carrying the ``args/{arg_key}/`` segment.
        """

    @abc.abstractmethod
    async def get(self, uri: str) -> str | None:
        """Return the content stored under ``uri``.

        Returns:
            - ``str`` content on hit.
            - ``None`` when the object is missing (S3 ``NoSuchKey`` / HTTP 404).

        Raises:
            ValueError: when ``uri`` is malformed (delegated to
                ``parse_tool_result_uri``).
            ClientError / other exceptions: on transport / auth / backend
                errors. The caller MUST distinguish these from ``None``.
        """

    async def list_keys(self, prefix: str) -> list[str]:
        """Return every object key stored directly under ``prefix``.

        Used by Task 5's ``recall_tool_result`` to find the
        ``{content_hash}.txt`` file for a given ``(tenant_id, task_id,
        tool_call_id[, args/{arg_key}])`` prefix. Returns an empty list when
        the prefix has no objects. Transport / auth / backend errors
        propagate so the caller can differentiate "no content" (missing)
        from "storage down" (transient).

        Default implementation returns ``[]``. Concrete stores that support
        listing (``S3ToolResultStore``, ``InMemoryToolResultStore``)
        override. Minimal test-double subclasses that only need put/get
        inherit the no-op behaviour safely.
        """
        return []


# ---------------------------------------------------------------------------
# InMemory implementation (tests)
# ---------------------------------------------------------------------------


class InMemoryToolResultStore(ToolResultArtifactStore):
    """Dict-backed test double. Keyed by the canonical URI string."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def put(
        self,
        *,
        tenant_id: str,
        task_id: str,
        tool_call_id: str,
        content: str,
        arg_key: str | None = None,
    ) -> str:
        uri_obj = _build_uri(
            tenant_id=tenant_id,
            task_id=task_id,
            tool_call_id=tool_call_id,
            content=content,
            arg_key=arg_key,
        )
        uri = uri_obj.to_uri()
        self._data[uri] = content
        return uri

    async def get(self, uri: str) -> str | None:
        # Parse first — exercises the shape gate.
        parse_tool_result_uri(uri)
        return self._data.get(uri)

    async def list_keys(self, prefix: str) -> list[str]:
        """Return keys whose URI begins with ``toolresult://{prefix}``.

        The production store would call ``list_objects_v2`` on the bucket;
        for the in-memory double we enumerate the dict's URI keys and strip
        the ``toolresult://`` scheme so the returned values are bucket-
        relative keys (same shape as the S3 implementation).
        """
        marker = f"{URI_SCHEME}{prefix}"
        return [
            uri[len(URI_SCHEME):]
            for uri in self._data.keys()
            if uri.startswith(marker)
        ]


# ---------------------------------------------------------------------------
# S3 implementation (production)
# ---------------------------------------------------------------------------


def _is_missing_key_error(err: ClientError) -> bool:
    """Return True when ``err`` represents an S3 missing-object condition.

    S3 surfaces ``NoSuchKey`` for ``GetObject`` against a missing key, and
    occasionally ``404 Not Found`` depending on bucket + request-style. Both
    map to "object does not exist" which the contract says should return
    ``None`` instead of propagating.
    """
    try:
        code = err.response.get("Error", {}).get("Code", "")
    except Exception:  # pragma: no cover — defensive
        code = ""
    if code in ("NoSuchKey", "404", "NotFound"):
        return True
    try:
        status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    except Exception:  # pragma: no cover — defensive
        status = None
    return status == 404


class S3ToolResultStore(ToolResultArtifactStore):
    """Production store backed by the existing :class:`S3Client`.

    Content-Type is always ``text/plain; charset=utf-8``. Keys under the
    ``platform-artifacts`` bucket mirror the URI path verbatim.
    """

    def __init__(self, s3_client: S3Client) -> None:
        self._s3 = s3_client

    async def put(
        self,
        *,
        tenant_id: str,
        task_id: str,
        tool_call_id: str,
        content: str,
        arg_key: str | None = None,
    ) -> str:
        uri_obj = _build_uri(
            tenant_id=tenant_id,
            task_id=task_id,
            tool_call_id=tool_call_id,
            content=content,
            arg_key=arg_key,
        )
        data = content.encode("utf-8")
        await self._s3.upload(
            key=uri_obj.to_key(),
            data=data,
            content_type=_CONTENT_TYPE,
        )
        return uri_obj.to_uri()

    async def get(self, uri: str) -> str | None:
        parsed = parse_tool_result_uri(uri)
        key = parsed.to_key()
        try:
            data = await self._s3.download(key)
        except ClientError as e:
            if _is_missing_key_error(e):
                return None
            raise
        return data.decode("utf-8")

    async def list_keys(self, prefix: str) -> list[str]:
        """Return object keys directly under ``prefix`` via ``list_objects_v2``.

        Uses the wrapped :class:`S3Client`'s boto3 client through
        ``asyncio.to_thread`` so the rest of the worker remains async-pure.
        Pagination is handled by the ``Paginator`` API — in practice a
        single tool_call_id rarely exceeds one page, but we iterate all
        pages defensively. Transport / auth errors propagate unchanged.
        """
        boto_client = self._s3._client  # type: ignore[attr-defined]
        bucket = self._s3.bucket_name

        def _list() -> list[str]:
            paginator = boto_client.get_paginator("list_objects_v2")
            out: list[str] = []
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for item in page.get("Contents", []) or []:
                    key = item.get("Key")
                    if isinstance(key, str):
                        out.append(key)
            return out

        return await asyncio.to_thread(_list)


__all__ = [
    "ToolResultURI",
    "ToolResultArtifactStore",
    "InMemoryToolResultStore",
    "S3ToolResultStore",
    "parse_tool_result_uri",
]
