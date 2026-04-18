"""Tests for :mod:`tools.task_history_reader`.

Covers the two failure modes the design doc calls out and the happy path:

- Happy path: AIMessage tool_calls paired with ToolMessage results by id.
- Content-block shape: Anthropic ``type: "tool_use"`` blocks inside message
  content.
- Orphan ToolMessage (no matching AIMessage) silently skipped.
- Missing checkpoint tuple → empty list.
- Malformed checkpoint structure → empty list + warning log.
- Truncation of args / result previews.
- Cap on total entries.
"""

from __future__ import annotations

import pytest

from tools.task_history_reader import read_tool_calls


class _FakeAIMessage:
    """AIMessage-shaped stub with a ``tool_calls`` attribute."""

    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls or []
        self.content = content


class _FakeToolMessage:
    """ToolMessage-shaped stub with a ``tool_call_id`` + content."""

    def __init__(self, tool_call_id: str, content):
        self.tool_call_id = tool_call_id
        self.content = content


class _FakeTuple:
    """LangGraph checkpointer returns a tuple-like with a ``checkpoint`` attr."""

    def __init__(self, checkpoint: dict):
        self.checkpoint = checkpoint


class _FakeCheckpointer:
    """Async checkpointer stub exposing only ``aget_tuple``."""

    def __init__(self, tup):
        self._tup = tup
        self.calls: list[dict] = []

    async def aget_tuple(self, config):
        self.calls.append(config)
        return self._tup


def _messages_tuple(messages):
    return _FakeTuple({"channel_values": {"messages": messages}})


# --- Happy paths --------------------------------------------------------


@pytest.mark.asyncio
async def test_read_tool_calls_pairs_ai_call_with_tool_result():
    ai = _FakeAIMessage(tool_calls=[
        {"id": "c-1", "name": "web_search", "args": {"q": "hello"}},
    ])
    tool_result = _FakeToolMessage("c-1", "Found 3 results")
    checkpointer = _FakeCheckpointer(_messages_tuple([ai, tool_result]))

    result = await read_tool_calls(
        checkpointer, "task-1", cap=20, preview_bytes=256
    )

    assert len(result) == 1
    assert result[0]["name"] == "web_search"
    assert result[0]["args_preview"] == '{"q": "hello"}'
    assert result[0]["result_preview"] == "Found 3 results"
    # Config passed thread_id correctly.
    assert checkpointer.calls == [{"configurable": {"thread_id": "task-1"}}]


@pytest.mark.asyncio
async def test_read_tool_calls_preserves_invocation_order():
    ai1 = _FakeAIMessage(tool_calls=[
        {"id": "c-1", "name": "first_tool", "args": {}},
    ])
    tm1 = _FakeToolMessage("c-1", "r1")
    ai2 = _FakeAIMessage(tool_calls=[
        {"id": "c-2", "name": "second_tool", "args": {"x": 42}},
    ])
    tm2 = _FakeToolMessage("c-2", "r2")
    checkpointer = _FakeCheckpointer(_messages_tuple([ai1, tm1, ai2, tm2]))

    result = await read_tool_calls(
        checkpointer, "task-1", cap=20, preview_bytes=256
    )

    assert [r["name"] for r in result] == ["first_tool", "second_tool"]


@pytest.mark.asyncio
async def test_read_tool_calls_handles_anthropic_content_blocks():
    """Anthropic provider can pass raw tool_use blocks via message.content."""
    ai = _FakeAIMessage(
        tool_calls=[],
        content=[
            {"type": "text", "text": "Let me search…"},
            {
                "type": "tool_use",
                "id": "c-1",
                "name": "web_search",
                "input": {"q": "hello"},
            },
        ],
    )
    tm = _FakeToolMessage("c-1", "ok")
    checkpointer = _FakeCheckpointer(_messages_tuple([ai, tm]))

    result = await read_tool_calls(
        checkpointer, "task-1", cap=20, preview_bytes=256
    )

    assert len(result) == 1
    assert result[0]["name"] == "web_search"
    assert result[0]["result_preview"] == "ok"


@pytest.mark.asyncio
async def test_read_tool_calls_does_not_dupe_when_both_shapes_present():
    """tool_calls + tool_use blocks for the same call id → one entry."""
    ai = _FakeAIMessage(
        tool_calls=[{"id": "c-1", "name": "calc", "args": {"a": 1}}],
        content=[
            {"type": "tool_use", "id": "c-1", "name": "calc", "input": {"a": 1}},
        ],
    )
    checkpointer = _FakeCheckpointer(_messages_tuple([ai]))

    result = await read_tool_calls(
        checkpointer, "task-1", cap=20, preview_bytes=256
    )

    assert len(result) == 1


# --- Truncation + cap ---------------------------------------------------


@pytest.mark.asyncio
async def test_read_tool_calls_truncates_args_and_result_previews():
    big = "x" * 2000
    ai = _FakeAIMessage(tool_calls=[
        {"id": "c-1", "name": "big_tool", "args": {"payload": big}},
    ])
    tm = _FakeToolMessage("c-1", big)
    checkpointer = _FakeCheckpointer(_messages_tuple([ai, tm]))

    result = await read_tool_calls(
        checkpointer, "task-1", cap=20, preview_bytes=64
    )

    assert len(result[0]["args_preview"]) == 64
    assert result[0]["args_preview"].endswith("…")
    assert len(result[0]["result_preview"]) == 64
    assert result[0]["result_preview"].endswith("…")


@pytest.mark.asyncio
async def test_read_tool_calls_caps_entry_count():
    messages = []
    for i in range(50):
        messages.append(_FakeAIMessage(tool_calls=[
            {"id": f"c-{i}", "name": f"tool_{i}", "args": {}},
        ]))
    checkpointer = _FakeCheckpointer(_messages_tuple(messages))

    result = await read_tool_calls(
        checkpointer, "task-1", cap=5, preview_bytes=64
    )

    assert len(result) == 5
    assert [r["name"] for r in result] == [f"tool_{i}" for i in range(5)]


# --- Degraded / missing states ------------------------------------------


@pytest.mark.asyncio
async def test_read_tool_calls_returns_empty_for_missing_checkpoint():
    checkpointer = _FakeCheckpointer(None)
    result = await read_tool_calls(
        checkpointer, "task-1", cap=20, preview_bytes=64
    )
    assert result == []


@pytest.mark.asyncio
async def test_read_tool_calls_returns_empty_for_missing_checkpointer():
    result = await read_tool_calls(None, "task-1", cap=20, preview_bytes=64)
    assert result == []


@pytest.mark.asyncio
async def test_read_tool_calls_returns_empty_for_missing_task_id():
    checkpointer = _FakeCheckpointer(_messages_tuple([]))
    result = await read_tool_calls(checkpointer, "", cap=20, preview_bytes=64)
    assert result == []


@pytest.mark.asyncio
async def test_read_tool_calls_returns_empty_when_messages_not_list():
    tup = _FakeTuple({"channel_values": {"messages": "garbage"}})
    checkpointer = _FakeCheckpointer(tup)

    result = await read_tool_calls(
        checkpointer, "task-1", cap=20, preview_bytes=64
    )

    assert result == []


@pytest.mark.asyncio
async def test_read_tool_calls_returns_empty_on_checkpointer_error(caplog):
    class _BrokenCheckpointer:
        async def aget_tuple(self, config):
            raise RuntimeError("simulated LangGraph format drift")

    with caplog.at_level("WARNING"):
        result = await read_tool_calls(
            _BrokenCheckpointer(), "task-1", cap=20, preview_bytes=64
        )

    assert result == []
    assert any(
        "tool_calls_read_failed" in rec.message for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_read_tool_calls_skips_orphan_tool_message():
    """ToolMessage whose id has no matching AIMessage call is silently dropped."""
    orphan = _FakeToolMessage("nonexistent-id", "stray result")
    checkpointer = _FakeCheckpointer(_messages_tuple([orphan]))

    result = await read_tool_calls(
        checkpointer, "task-1", cap=20, preview_bytes=64
    )

    assert result == []


@pytest.mark.asyncio
async def test_read_tool_calls_handles_list_content_tool_result():
    """Tool results returned as [{type: 'text', text: '…'}] blocks are flattened."""
    ai = _FakeAIMessage(tool_calls=[
        {"id": "c-1", "name": "tool", "args": {}},
    ])
    tm = _FakeToolMessage("c-1", [
        {"type": "text", "text": "part 1"},
        {"type": "text", "text": "part 2"},
    ])
    checkpointer = _FakeCheckpointer(_messages_tuple([ai, tm]))

    result = await read_tool_calls(
        checkpointer, "task-1", cap=20, preview_bytes=256
    )

    assert result[0]["result_preview"] == "part 1\npart 2"
