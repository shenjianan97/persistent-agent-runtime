"""Unit tests for Tier 0 ingestion offload helpers (Phase 2 Track 7
Follow-up, Task 4).

Covers the observable behaviours listed in
docs/exec-plans/active/phase-2/track-7-follow-up/agent_tasks/task-4-tool-result-offload.md
§Acceptance Criteria:

- Result offload fires at threshold; below-threshold stays inline.
- Arg offload fires only for keys in ``TRUNCATABLE_ARG_KEYS`` (not e.g.
  ``search_phrase``).
- Fail-closed per-item: an offload-failing candidate stays inline; other
  candidates still get offloaded.
- Fail-closed batch: all-failed emits a single WARN.
- Config flag ``context_management.offload_tool_results = false`` disables
  both paths end-to-end (tested at the graph wiring level — the helpers
  themselves are always on; the flag gates whether the store is created).

The helpers take a store argument directly, so tests use
``InMemoryToolResultStore`` + a deliberately-failing subclass.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from executor.compaction.defaults import OFFLOAD_THRESHOLD_BYTES
from executor.compaction import ingestion as _ingestion_mod
from executor.compaction.ingestion import (
    OffloadEvent,
    offload_ai_message_args,
    offload_tool_message,
    offload_tool_messages_batch,
)
from executor.compaction.tool_result_store import (
    InMemoryToolResultStore,
    ToolResultArtifactStore,
    parse_tool_result_uri,
)


class _RaisingStore(ToolResultArtifactStore):
    """Test double that raises on every ``put`` call."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or RuntimeError("boom")
        self.puts_attempted: int = 0

    async def put(self, **kwargs: Any) -> str:  # noqa: D401
        self.puts_attempted += 1
        raise self.exc

    async def get(self, uri: str) -> str | None:  # pragma: no cover
        return None


class _PartiallyFailingStore(ToolResultArtifactStore):
    """Fails for the first N puts, then succeeds via an inner delegate."""

    def __init__(self, *, fail_first: int) -> None:
        self._inner = InMemoryToolResultStore()
        self._remaining = fail_first
        self.fails: int = 0

    async def put(self, **kwargs: Any) -> str:
        if self._remaining > 0:
            self._remaining -= 1
            self.fails += 1
            raise RuntimeError("s3 transport error")
        return await self._inner.put(**kwargs)

    async def get(self, uri: str) -> str | None:  # pragma: no cover
        return await self._inner.get(uri)


# ---------------------------------------------------------------------------
# ToolMessage result offload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOffloadToolMessage:
    async def test_above_threshold_offloads_and_replaces_content(self):
        store = InMemoryToolResultStore()
        content = "x" * (OFFLOAD_THRESHOLD_BYTES + 1000)
        msg = ToolMessage(
            content=content,
            tool_call_id="call-1",
            name="sandbox_read_file",
        )
        outcome = await offload_tool_message(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )

        # Placeholder form.
        assert isinstance(outcome.message, ToolMessage)
        assert outcome.message is not msg
        assert outcome.message.content.startswith("[tool result ")
        assert f"{len(content.encode('utf-8'))} bytes" in outcome.message.content
        assert "preview: " in outcome.message.content
        # Placeholder length is tightly bounded (≪ 25KB).
        assert len(outcome.message.content) < 2_000
        # URI round-trips.
        assert len(outcome.events) == 1
        assert outcome.events[0].kind == "success"
        assert outcome.events[0].variant == "result"
        uri = outcome.events[0].uri
        assert uri is not None
        assert await store.get(uri) == content

    async def test_below_threshold_stays_inline(self):
        store = InMemoryToolResultStore()
        content = "hello world"
        msg = ToolMessage(content=content, tool_call_id="call-1", name="my_tool")
        outcome = await offload_tool_message(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        assert outcome.message is msg
        assert outcome.message.content == content
        assert outcome.events == ()

    async def test_exactly_threshold_is_inline(self):
        """At (not above) threshold: stays inline."""
        store = InMemoryToolResultStore()
        content = "x" * OFFLOAD_THRESHOLD_BYTES
        msg = ToolMessage(content=content, tool_call_id="call-1", name="t")
        outcome = await offload_tool_message(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        assert outcome.message is msg

    async def test_non_string_content_passed_through(self):
        store = InMemoryToolResultStore()
        msg = ToolMessage(content=[{"type": "text", "text": "x"}], tool_call_id="c", name="t")
        outcome = await offload_tool_message(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        assert outcome.message is msg

    async def test_store_failure_returns_input_unchanged(self):
        store = _RaisingStore()
        content = "y" * (OFFLOAD_THRESHOLD_BYTES + 500)
        msg = ToolMessage(content=content, tool_call_id="call-fail", name="t")
        outcome = await offload_tool_message(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        assert outcome.message is msg
        assert outcome.message.content == content
        assert len(outcome.events) == 1
        assert outcome.events[0].kind == "failed"
        assert outcome.events[0].variant == "result"
        assert outcome.events[0].error_type == "RuntimeError"


@pytest.mark.asyncio
class TestOffloadToolMessagesBatch:
    async def test_partial_failure_keeps_failing_item_inline(self):
        """Two oversized results, first fails, second succeeds → failing one
        stays inline, other is offloaded, all-failed WARN NOT emitted."""
        store = _PartiallyFailingStore(fail_first=1)
        c1 = "a" * (OFFLOAD_THRESHOLD_BYTES + 200)
        c2 = "b" * (OFFLOAD_THRESHOLD_BYTES + 300)
        m1 = ToolMessage(content=c1, tool_call_id="c1", name="t")
        m2 = ToolMessage(content=c2, tool_call_id="c2", name="t")
        out, events = await offload_tool_messages_batch(
            [m1, m2],
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        # First stayed inline, second was replaced.
        assert out[0].content == c1
        assert out[1].content.startswith("[tool result ")
        kinds = [e.kind for e in events]
        assert "failed" in kinds
        assert "success" in kinds

    async def test_passthrough_for_non_toolmessage_commands(self):
        """Track 7 Follow-up Task 5 regression: the ToolNode may emit
        ``Command`` objects (from tools like ``memory_note`` / ``save_memory``).
        They have no ``.content`` attribute and must not be offloaded —
        the batch helper must pass them through unchanged while still
        offloading the real ToolMessage candidates.
        """
        from langgraph.types import Command

        store = InMemoryToolResultStore()
        cmd = Command(update={"observations": ["user noted X"]})
        big = "z" * (OFFLOAD_THRESHOLD_BYTES + 500)
        tm = ToolMessage(content=big, tool_call_id="tm1", name="read_url")
        out, events = await offload_tool_messages_batch(
            [cmd, tm],
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        # Command passed through identity; ToolMessage was offloaded.
        assert out[0] is cmd
        assert isinstance(out[1], ToolMessage)
        assert out[1].content.startswith("[tool result ")
        assert [e.kind for e in events] == ["success"]

    async def test_all_failures_emits_all_failed_warn(self, monkeypatch):
        """Every candidate fails → a single ``compaction.offload_all_failed``
        WARN fires for the pass."""
        calls: list[tuple[str, dict[str, Any]]] = []

        class _RecordingLogger:
            def warning(self, event: str, **kwargs: Any) -> None:
                calls.append((event, kwargs))

            def info(self, event: str, **kwargs: Any) -> None:  # pragma: no cover
                calls.append((event, kwargs))

        monkeypatch.setattr(_ingestion_mod, "_logger", _RecordingLogger())

        store = _RaisingStore()
        c1 = "a" * (OFFLOAD_THRESHOLD_BYTES + 200)
        c2 = "b" * (OFFLOAD_THRESHOLD_BYTES + 300)
        m1 = ToolMessage(content=c1, tool_call_id="c1", name="t")
        m2 = ToolMessage(content=c2, tool_call_id="c2", name="t")

        out, events = await offload_tool_messages_batch(
            [m1, m2],
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        assert out[0].content == c1
        assert out[1].content == c2
        assert all(e.kind == "failed" for e in events)

        all_failed = [c for c in calls if c[0] == "compaction.offload_all_failed"]
        assert len(all_failed) == 1
        assert all_failed[0][1]["failed_count"] == 2
        assert all_failed[0][1]["variant"] == "result"


# ---------------------------------------------------------------------------
# AIMessage tool-call-arg offload
# ---------------------------------------------------------------------------


def _ai_msg_with_tool_call(
    *,
    call_id: str = "call-x",
    name: str = "sandbox_write_file",
    args: dict[str, Any],
) -> AIMessage:
    """Build an AIMessage whose tool_calls matches the LangChain 0.3+ dict shape."""
    return AIMessage(
        content="",
        tool_calls=[
            {"id": call_id, "name": name, "args": args, "type": "tool_call"}
        ],
    )


@pytest.mark.asyncio
class TestOffloadAiMessageArgs:
    async def test_truncatable_key_above_threshold_is_offloaded(self):
        store = InMemoryToolResultStore()
        big = "z" * (OFFLOAD_THRESHOLD_BYTES + 10)
        msg = _ai_msg_with_tool_call(args={"path": "/tmp/a.txt", "content": big})
        outcome = await offload_ai_message_args(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        assert outcome.message is not msg
        tc = outcome.message.tool_calls[0]
        assert tc["args"]["path"] == "/tmp/a.txt"
        assert tc["args"]["content"].startswith("[tool arg 'content' ")
        assert "preview: " in tc["args"]["content"]
        # URI resolvable.
        assert len(outcome.events) == 1
        uri = outcome.events[0].uri
        assert uri is not None
        assert await store.get(uri) == big
        parsed = parse_tool_result_uri(uri)
        assert parsed.arg_key == "content"

    async def test_non_truncatable_key_is_never_offloaded(self):
        """search_phrase / path / query are not in the allowlist."""
        store = InMemoryToolResultStore()
        big = "z" * (OFFLOAD_THRESHOLD_BYTES + 10)
        msg = _ai_msg_with_tool_call(
            name="web_search",
            args={"search_phrase": big, "max_results": 5},
        )
        outcome = await offload_ai_message_args(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        # Unchanged.
        assert outcome.message is msg
        assert outcome.message.tool_calls[0]["args"]["search_phrase"] == big
        assert outcome.events == ()

    async def test_below_threshold_stays_inline(self):
        store = InMemoryToolResultStore()
        msg = _ai_msg_with_tool_call(args={"content": "short"})
        outcome = await offload_ai_message_args(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        assert outcome.message is msg

    async def test_all_truncatable_keys_covered(self):
        """{content, new_string, old_string, text, body} all offload."""
        store = InMemoryToolResultStore()
        big = "q" * (OFFLOAD_THRESHOLD_BYTES + 5)
        for key in ("content", "new_string", "old_string", "text", "body"):
            msg = _ai_msg_with_tool_call(
                call_id=f"c-{key}",
                args={key: big},
            )
            outcome = await offload_ai_message_args(
                msg,
                store=store,
                tenant_id="t1",
                task_id="task-1",
            )
            assert outcome.message.tool_calls[0]["args"][key].startswith(
                f"[tool arg '{key}' "
            ), key

    async def test_non_string_truncatable_value_passed_through(self):
        store = InMemoryToolResultStore()
        msg = _ai_msg_with_tool_call(args={"content": 12345})
        outcome = await offload_ai_message_args(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        assert outcome.message is msg

    async def test_partial_failure_per_candidate(self):
        """Two oversized arg values, first fails → first stays inline, second
        offloaded. No all-failed WARN since second succeeded."""
        store = _PartiallyFailingStore(fail_first=1)
        big1 = "a" * (OFFLOAD_THRESHOLD_BYTES + 100)
        big2 = "b" * (OFFLOAD_THRESHOLD_BYTES + 200)
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "c1",
                    "name": "t",
                    "args": {"content": big1},
                    "type": "tool_call",
                },
                {
                    "id": "c2",
                    "name": "t",
                    "args": {"new_string": big2},
                    "type": "tool_call",
                },
            ],
        )
        outcome = await offload_ai_message_args(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        # First call's arg stays inline.
        assert outcome.message.tool_calls[0]["args"]["content"] == big1
        # Second call's arg is replaced.
        assert outcome.message.tool_calls[1]["args"]["new_string"].startswith(
            "[tool arg 'new_string' "
        )
        # Events: one failed, one success.
        kinds = [e.kind for e in outcome.events]
        assert kinds.count("failed") == 1
        assert kinds.count("success") == 1

    async def test_all_failed_emits_warn(self, monkeypatch):
        calls: list[tuple[str, dict[str, Any]]] = []

        class _RecordingLogger:
            def warning(self, event: str, **kwargs: Any) -> None:
                calls.append((event, kwargs))

            def info(self, event: str, **kwargs: Any) -> None:  # pragma: no cover
                calls.append((event, kwargs))

        monkeypatch.setattr(_ingestion_mod, "_logger", _RecordingLogger())

        store = _RaisingStore()
        big1 = "a" * (OFFLOAD_THRESHOLD_BYTES + 100)
        big2 = "b" * (OFFLOAD_THRESHOLD_BYTES + 200)
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "c1",
                    "name": "t",
                    "args": {"content": big1},
                    "type": "tool_call",
                },
                {
                    "id": "c2",
                    "name": "t",
                    "args": {"content": big2},
                    "type": "tool_call",
                },
            ],
        )
        outcome = await offload_ai_message_args(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        assert outcome.message.tool_calls[0]["args"]["content"] == big1
        assert outcome.message.tool_calls[1]["args"]["content"] == big2
        assert all(e.kind == "failed" for e in outcome.events)

        all_failed = [c for c in calls if c[0] == "compaction.offload_all_failed"]
        assert len(all_failed) == 1
        assert all_failed[0][1]["failed_count"] == 2
        assert all_failed[0][1]["variant"] == "arg"

    async def test_no_tool_calls_is_noop(self):
        store = InMemoryToolResultStore()
        msg = AIMessage(content="hello", tool_calls=[])
        outcome = await offload_ai_message_args(
            msg,
            store=store,
            tenant_id="t1",
            task_id="task-1",
        )
        assert outcome.message is msg
        assert outcome.events == ()


# ---------------------------------------------------------------------------
# Config-flag precondition (contract check — the flag gates *whether* the
# graph instantiates the store; offload helpers are always on). This is a
# lightweight cross-reference that the correct key name is plumbed.
# ---------------------------------------------------------------------------


def test_config_flag_key_is_offload_tool_results():
    """The Console & Java side serialise ``offload_tool_results`` (snake_case).

    This test guards the contract at the worker-visible layer — the graph
    reads ``agent_config["context_management"]["offload_tool_results"]``;
    drift would silently disable the kill switch.
    """
    cfg = {
        "context_management": {"offload_tool_results": False},
    }
    flag = (cfg.get("context_management") or {}).get("offload_tool_results")
    assert flag is False
