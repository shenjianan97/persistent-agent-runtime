"""Unit tests for Phase 2 Track 7 Follow-up Task 5 — ``recall_tool_result``.

Covered contracts (see
docs/exec-plans/active/phase-2/track-7-follow-up/agent_tasks/task-5-recall-tool-result.md):

* Happy path — a valid ``(tool_call_id[, arg_key])`` in the same task
  returns the content ``store.put`` originally wrote, byte-for-byte.
* Malformed input — empty / whitespace ``tool_call_id`` returns
  ``ERROR_MALFORMED`` without touching the store.
* Cross-task rejection — even a URI spoofed from a different task / tenant
  cannot coerce the tool into fetching it; the closure-bound scope wins.
* ``store.get`` returns ``None`` — recoverable "content purged" error.
* ``store.get`` RAISES — "artifact store temporarily unavailable" + WARN
  log, tool does not raise.
* Special ingestion rule — the recall tool's own ToolMessage carries
  ``additional_kwargs.recalled=True`` and ``original_tool_call_id`` and
  BYPASSES Task 4's offload (covered via the graph's tool-node wrapper in
  ``test_graph_recall_ingestion_bypass``).
* Graph-level registration gating — ``offload_tool_results=true`` registers
  the tool AND includes the system-prompt hint; ``false`` does neither.
  This is covered via a small ``_build_platform_system_message`` sanity
  test that doesn't require spinning up a full graph.

These tests deliberately do NOT hit Postgres, S3, or the network — the
store is the in-memory double; logs are captured with ``caplog``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from executor.builtin_tools.recall_tool_result import (
    ERROR_AMBIGUOUS,
    ERROR_CROSS_TASK,
    ERROR_MALFORMED,
    ERROR_NO_HASH,
    ERROR_PURGED,
    ERROR_TRANSIENT,
    RECALL_TOOL_RESULT_DESCRIPTION,
    RECALL_TOOL_RESULT_NAME,
    RECALL_TOOL_RESULT_SYSTEM_PROMPT_HINT,
    _resolve_and_fetch,
    build_recall_tool_result_tool,
)
from executor.builtin_tools.recall_tool_result import _RecallContext
from executor.compaction.tool_result_store import (
    InMemoryToolResultStore,
    ToolResultArtifactStore,
)


TENANT = "tenant-a"
TASK = "task-a"
TOOL_CALL_ID = "tooluse_abc123"


@pytest.fixture
def store() -> InMemoryToolResultStore:
    return InMemoryToolResultStore()


def _ctx(
    store: ToolResultArtifactStore, tenant: str = TENANT, task: str = TASK
) -> _RecallContext:
    return _RecallContext(tenant_id=tenant, task_id=task, store=store)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_tool_result_returns_offloaded_content_byte_for_byte(
    store: InMemoryToolResultStore,
):
    content = "line 1\nline 2\n" + "x" * 50_000  # well over threshold, but
    # size doesn't matter here — we're testing round-trip equality.
    await store.put(
        tenant_id=TENANT,
        task_id=TASK,
        tool_call_id=TOOL_CALL_ID,
        content=content,
    )

    out = await _resolve_and_fetch(
        ctx=_ctx(store), tool_call_id=TOOL_CALL_ID, arg_key=None
    )

    assert out == content


@pytest.mark.asyncio
async def test_recall_tool_result_arg_side_round_trip(
    store: InMemoryToolResultStore,
):
    content = "hello world\n" * 10_000
    await store.put(
        tenant_id=TENANT,
        task_id=TASK,
        tool_call_id=TOOL_CALL_ID,
        content=content,
        arg_key="content",
    )

    out = await _resolve_and_fetch(
        ctx=_ctx(store), tool_call_id=TOOL_CALL_ID, arg_key="content"
    )

    assert out == content


# ---------------------------------------------------------------------------
# Malformed / missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_tool_call_id_returns_malformed_without_calling_store(
    store: InMemoryToolResultStore,
):
    calls: list[str] = []

    async def _spy_list(prefix: str) -> list[str]:  # noqa: ARG001
        calls.append(prefix)
        return []

    # Replace the listing method to assert zero hits.
    store.list_keys = _spy_list  # type: ignore[method-assign]

    for bad in ("", "   ", None):  # None is the Pydantic "missing" case
        out = await _resolve_and_fetch(
            ctx=_ctx(store),
            tool_call_id=bad,  # type: ignore[arg-type]
            arg_key=None,
        )
        assert out == ERROR_MALFORMED

    assert calls == []


@pytest.mark.asyncio
async def test_missing_tool_call_id_returns_no_hash(
    store: InMemoryToolResultStore,
):
    # Store is empty for this tool_call_id AND empty for the whole task, so
    # the diagnostic listing is empty and the response is the base constant.
    out = await _resolve_and_fetch(
        ctx=_ctx(store), tool_call_id="tooluse_unknown", arg_key=None
    )
    assert out == ERROR_NO_HASH


@pytest.mark.asyncio
async def test_missing_id_lists_available_ids_when_store_has_other_entries(
    store: InMemoryToolResultStore,
):
    """Regression for task 75f5a223: model called ``recall_tool_result`` with
    a stripped id (``"vIEO..."``) while the real id was
    ``"tooluse_vIEO..."``. The old response was an opaque "not found" string
    that gave the model no signal to self-correct. The enriched response
    echoes the input and lists the ids that DO exist for this task so the
    model can spot the mismatch on the next turn.

    We deliberately do NOT auto-correct the id — the model needs to see its
    own mistake to learn from it. This test asserts the diagnostic shape,
    not any fuzzy-matching behaviour."""
    # Seed the store with two real offloads under this task.
    await store.put(
        tenant_id=TENANT,
        task_id=TASK,
        tool_call_id="tooluse_real_one",
        content="A" * 100,
    )
    await store.put(
        tenant_id=TENANT,
        task_id=TASK,
        tool_call_id="tooluse_real_two",
        content="B" * 100,
        arg_key="content",
    )

    out = await _resolve_and_fetch(
        # Drop the ``tooluse_`` prefix — the actual mistake observed in
        # production.
        ctx=_ctx(store),
        tool_call_id="real_one",
        arg_key=None,
    )

    # Base error message is preserved — existing string-equality callers
    # looking for the substring still work.
    assert out.startswith(ERROR_NO_HASH)
    # Echoes what the model supplied, so it sees its own wrong argument.
    assert "'real_one'" in out
    # Lists the available ids, INCLUDING the one the model probably meant.
    assert "tooluse_real_one" in out
    # And the arg-side entry, with its arg_key annotated.
    assert "tooluse_real_two" in out
    assert "arg_key='content'" in out
    # Does NOT silently claim success.
    assert "A" * 100 not in out
    assert "B" * 100 not in out


@pytest.mark.asyncio
async def test_missing_id_diagnostic_listing_is_bounded(
    store: InMemoryToolResultStore,
):
    """When a task has many offloads, the diagnostic listing is capped so
    the error response stays bounded in size (no multi-KB error text)."""
    # Seed 50 entries; cap is 20.
    for i in range(50):
        await store.put(
            tenant_id=TENANT,
            task_id=TASK,
            tool_call_id=f"tooluse_entry_{i:03d}",
            content=f"payload-{i}",
        )

    out = await _resolve_and_fetch(
        ctx=_ctx(store), tool_call_id="does_not_exist", arg_key=None
    )
    # Count lines naming a tool_call_id. The response format is
    # "  - tool_call_id='...'" one per entry.
    listed = sum(1 for line in out.splitlines() if "tool_call_id='" in line)
    # One line is the "You supplied" echo; the rest are the listing.
    assert 1 < listed <= 21


# ---------------------------------------------------------------------------
# Cross-task rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_task_tool_call_id_is_rejected(
    store: InMemoryToolResultStore, caplog: pytest.LogCaptureFixture
):
    # Write the artefact under a DIFFERENT task.
    await store.put(
        tenant_id=TENANT,
        task_id="task-OTHER",
        tool_call_id=TOOL_CALL_ID,
        content="secret payload",
    )

    # Ctx is bound to task-a but the agent tries to recall an id that only
    # exists under task-OTHER. The tool must NOT hit the store with the
    # cross-task URI — the list prefix is derived from the bound task, so
    # it will simply miss (NO_HASH) rather than returning the secret.
    out = await _resolve_and_fetch(
        ctx=_ctx(store), tool_call_id=TOOL_CALL_ID, arg_key=None
    )
    assert out == ERROR_NO_HASH

    # Belt-and-suspenders: force a cross-task URI through the parse gate
    # via the scope check by listing the other-task prefix directly. We
    # verify the gate fires by temporarily swapping the list method.
    async def _return_other_task_key(prefix: str) -> list[str]:  # noqa: ARG001
        return [f"task-OTHER/{TOOL_CALL_ID}/deadbeefdead.txt"]

    store.list_keys = _return_other_task_key  # type: ignore[method-assign]

    caplog.set_level(logging.WARNING)
    out = await _resolve_and_fetch(
        ctx=_ctx(store), tool_call_id=TOOL_CALL_ID, arg_key=None
    )
    # Even when the listing erroneously hands us a cross-task hash we
    # reconstruct the URI under the CURRENT ctx, so the parsed tenant/task
    # always match ctx. The lookup will then miss (ERROR_PURGED) because
    # the canonical URI under ctx has no backing bytes. The guard still
    # never fetches cross-task artefacts.
    assert out in {ERROR_CROSS_TASK, ERROR_PURGED, ERROR_NO_HASH}


# ---------------------------------------------------------------------------
# store.get returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_get_returns_none_yields_purged_error(
    store: InMemoryToolResultStore,
):
    # Make the listing return a realistic hash file but the URI isn't
    # actually present in ``store._data``.
    fake_hash = "0123456789ab"

    async def _list(prefix: str) -> list[str]:
        return [f"{prefix}{fake_hash}.txt"]

    store.list_keys = _list  # type: ignore[method-assign]
    out = await _resolve_and_fetch(
        ctx=_ctx(store), tool_call_id=TOOL_CALL_ID, arg_key=None
    )
    assert out == ERROR_PURGED


# ---------------------------------------------------------------------------
# store.get raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_get_raises_yields_transient_error(
    store: InMemoryToolResultStore, caplog: pytest.LogCaptureFixture
):
    fake_hash = "feedfacefeed"

    async def _list(prefix: str) -> list[str]:
        return [f"{prefix}{fake_hash}.txt"]

    class _BoomError(RuntimeError):
        pass

    async def _boom(uri: str) -> str | None:  # noqa: ARG001
        raise _BoomError("s3 unreachable")

    store.list_keys = _list  # type: ignore[method-assign]
    store.get = _boom  # type: ignore[method-assign]

    caplog.set_level(logging.WARNING)
    out = await _resolve_and_fetch(
        ctx=_ctx(store), tool_call_id=TOOL_CALL_ID, arg_key=None
    )
    assert out == ERROR_TRANSIENT
    # WARN log with error_type recorded.
    warn_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "recall_tool_result.fetch_failed" in r.message
    ]
    assert warn_records, "expected recall_tool_result.fetch_failed WARN"
    # Transport failures are caught during list OR get — either path is
    # structurally a "fetch_failed" event from the agent's perspective.
    record = warn_records[0]
    assert getattr(record, "error_type", None) == "_BoomError"


@pytest.mark.asyncio
async def test_store_list_raises_yields_transient_error(
    store: InMemoryToolResultStore, caplog: pytest.LogCaptureFixture
):
    class _BoomError(RuntimeError):
        pass

    async def _list(prefix: str) -> list[str]:  # noqa: ARG001
        raise _BoomError("s3 list outage")

    store.list_keys = _list  # type: ignore[method-assign]

    caplog.set_level(logging.WARNING)
    out = await _resolve_and_fetch(
        ctx=_ctx(store), tool_call_id=TOOL_CALL_ID, arg_key=None
    )
    assert out == ERROR_TRANSIENT


# ---------------------------------------------------------------------------
# Ambiguous (multiple hashes per prefix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_hashes_per_prefix_yields_ambiguous_error(
    store: InMemoryToolResultStore,
):
    # Write two distinct contents under the same tool_call_id (simulating
    # a provider-level retry emitting two offloads).
    await store.put(
        tenant_id=TENANT,
        task_id=TASK,
        tool_call_id=TOOL_CALL_ID,
        content="version one",
    )
    await store.put(
        tenant_id=TENANT,
        task_id=TASK,
        tool_call_id=TOOL_CALL_ID,
        content="version two",
    )
    out = await _resolve_and_fetch(
        ctx=_ctx(store), tool_call_id=TOOL_CALL_ID, arg_key=None
    )
    assert out == ERROR_AMBIGUOUS


# ---------------------------------------------------------------------------
# LangChain tool shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_recall_tool_result_tool_exposes_name_and_description(
    store: InMemoryToolResultStore,
):
    tool = build_recall_tool_result_tool(
        tenant_id=TENANT, task_id=TASK, store=store
    )
    assert tool.name == RECALL_TOOL_RESULT_NAME
    assert tool.description == RECALL_TOOL_RESULT_DESCRIPTION
    # arg_key is optional; ensure the schema validates {tool_call_id only}.
    schema_fields = set(tool.args_schema.model_fields.keys())  # type: ignore[union-attr]
    assert "tool_call_id" in schema_fields
    assert "arg_key" in schema_fields


@pytest.mark.asyncio
async def test_tool_never_raises_on_bad_input(
    store: InMemoryToolResultStore,
):
    tool = build_recall_tool_result_tool(
        tenant_id=TENANT, task_id=TASK, store=store
    )
    # Direct coroutine invocation bypasses the schema; we want to confirm
    # the underlying fn catches every failure mode.
    coro = tool.coroutine
    assert coro is not None
    out = await coro(tool_call_id="   ")  # type: ignore[misc]
    assert out == ERROR_MALFORMED


# ---------------------------------------------------------------------------
# System-prompt hint presence
# ---------------------------------------------------------------------------


def test_system_prompt_hint_mentions_recall_and_placeholder_shape():
    hint = RECALL_TOOL_RESULT_SYSTEM_PROMPT_HINT
    assert "recall_tool_result" in hint
    # Placeholder format recognisable by the agent
    assert "toolresult://" in hint
    # Context-budget reminder
    assert "context budget" in hint or "counts toward" in hint
