"""Unit tests for Phase 2 Track 5 Task 7 — worker-side memory tools.

Covered contracts:

- ``memory_note`` validates its argument (1..2048 chars), returns a
  ``Command(update={"observations": [text]})`` so LangGraph's
  ``operator.add`` reducer appends durably. No DB / network calls.
- ``memory_search`` delegates to ``GET /v1/agents/{bound_agent}/memory/search``;
  the bound ``agent_id`` comes from the worker task context and cannot be
  overridden by LLM arguments. Mode enforcement; 503 → recoverable tool
  error; 404 → empty result set; pass-through of ``ranking_used``.
- ``task_history_get`` queries ``tasks`` + ``agent_memory_entries`` with
  both ``tenant_id`` + ``agent_id`` in the WHERE clause; scope miss or
  malformed UUID → ``MemoryToolNotFoundError``; happy path returns a
  bounded structured view with truncation.
- ``build_memory_tools`` gating:
    * memory-enabled → all three tools returned (``memory_note``,
      ``memory_search``, ``task_history_get``).
    * memory-disabled → only ``task_history_get``.

These tests cover the Task 7 acceptance criteria without touching Postgres
or the network — asyncpg pool and ``httpx.AsyncClient`` are stubbed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from langgraph.types import Command
from pydantic import ValidationError

import asyncpg

from tools.memory_tools import (
    MEMORY_NOTE_MAX_LEN,
    MEMORY_SEARCH_TOOL_LIMIT_MAX,
    SAVE_MEMORY_REASON_MAX_LEN,
    MemoryNoteArguments,
    MemorySearchArguments,
    MemorySearchVectorUnavailableError,
    MemoryToolContext,
    MemoryToolError,
    MemoryToolNotFoundError,
    SaveMemoryArguments,
    TaskHistoryGetArguments,
    build_memory_tools,
)


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakePool:
    """Stand-in for asyncpg.Pool exposing just ``fetchrow``."""

    def __init__(self, *, fetchrow_return: Any = None, fetchrow_raises: Exception | None = None):
        self.fetchrow_return = fetchrow_return
        self.fetchrow_raises = fetchrow_raises
        self.calls: list[tuple[Any, ...]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, *args))
        if self.fetchrow_raises is not None:
            raise self.fetchrow_raises
        return self.fetchrow_return


class _FakeHttpResponse:
    def __init__(self, *, status_code: int, json_payload: Any = None, raise_on_json: bool = False):
        self.status_code = status_code
        self._payload = json_payload
        self._raise_on_json = raise_on_json

    def json(self) -> Any:
        if self._raise_on_json:
            raise ValueError("not json")
        return self._payload


class _FakeHttpClient:
    """Captures GET calls so tests can assert URL / params / bindings."""

    def __init__(self, responses: list[_FakeHttpResponse] | Exception):
        self.responses = responses
        self.get_calls: list[dict[str, Any]] = []

    async def get(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        self.get_calls.append({"url": url, "params": dict(params or {})})
        if isinstance(self.responses, Exception):
            raise self.responses
        return self.responses.pop(0)


def _make_ctx(
    *,
    tenant_id: str = "default",
    agent_id: str = "agent-A",
    task_id: str = "task-123",
    pool: Any | None = None,
    http_client: Any | None = None,
    base_url: str = "http://api.internal:8080",
    checkpointer: Any | None = None,
) -> MemoryToolContext:
    return MemoryToolContext(
        tenant_id=tenant_id,
        agent_id=agent_id,
        task_id=task_id,
        pool=pool if pool is not None else _FakePool(),
        memory_api_base_url=base_url,
        http_client=http_client if http_client is not None else _FakeHttpClient([]),
        checkpointer=checkpointer,
    )


def _tool_by_name(tools: list[Any], name: str) -> Any:
    for tool in tools:
        if tool.name == name:
            return tool
    raise KeyError(name)


# ---------------------------------------------------------------------------
# memory_note
# ---------------------------------------------------------------------------


class TestMemoryNoteArguments:
    def test_accepts_normal_text(self) -> None:
        parsed = MemoryNoteArguments(
            text="Observed that X correlates with Y", tool_call_id="call_x"
        )
        assert parsed.text.startswith("Observed")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            MemoryNoteArguments(text="", tool_call_id="call_x")

    def test_rejects_over_limit(self) -> None:
        with pytest.raises(ValidationError):
            MemoryNoteArguments(
                text="a" * (MEMORY_NOTE_MAX_LEN + 1), tool_call_id="call_x"
            )


def _invoke_with_tool_call(
    tool,
    args: dict,
    tool_call_id: str = "call_test",
    *,
    observations: list[str] | None = None,
):
    """Invoke a memory tool's underlying handler with both LLM args and the
    graph-state values that ``InjectedState`` would inject in production.

    ``StructuredTool.invoke(<tool_call_envelope>)`` populates
    ``InjectedToolCallId`` from the envelope but does NOT populate
    ``InjectedState`` — that work normally happens inside LangGraph's
    ``ToolNode``. For unit coverage we bypass ToolNode and call the
    underlying handler directly, passing ``observations`` as a keyword
    arg to match the injection the production path performs.

    ``observations`` defaults to an empty list — matches a fresh task
    state before any ``note_finding`` call has run.
    """
    kwargs = dict(args)
    kwargs["tool_call_id"] = tool_call_id
    # note_finding / save_memory both take ``observations`` via InjectedState.
    # memory_search / task_history_get do not; only pass it when the handler
    # signature accepts it.
    import inspect

    params = inspect.signature(tool.func).parameters
    if "observations" in params:
        kwargs["observations"] = list(observations or [])
    return tool.func(**kwargs)


class TestMemoryNoteTool:
    """Coverage for the canonical ``note_finding`` tool plus the
    ``memory_note`` alias retained for backward compatibility (issue #102).
    """

    def test_note_finding_returns_command_with_tool_message_and_observation(self) -> None:
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=True)
        tool = _tool_by_name(tools, "note_finding")

        result = _invoke_with_tool_call(tool, {"text": "hello"}, "call_xyz")
        assert isinstance(result, Command)
        assert result.update["observations"] == ["hello"]
        # LangGraph requires a matching ToolMessage paired to the tool_call_id.
        messages = result.update["messages"]
        assert len(messages) == 1
        assert messages[0].tool_call_id == "call_xyz"
        # Informative return — gives the agent direct evidence the call
        # landed, which fixes the hedging behaviour documented in #102.
        # Informative return in the new "#N, including this call" shape so
        # parallel-super-step siblings don't look like a stuck counter.
        assert "#1" in messages[0].content
        assert "including this call" in messages[0].content

    def test_note_finding_count_reflects_pre_update_state(self) -> None:
        """Count in the ToolMessage is ``len(observations) + 1``."""
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=True)
        tool = _tool_by_name(tools, "note_finding")

        result = _invoke_with_tool_call(
            tool, {"text": "third"}, observations=["first", "second"]
        )
        assert "#3" in result.update["messages"][0].content

    def test_memory_note_alias_logs_deprecation(self, caplog) -> None:
        """The deprecated alias routes to the same handler but emits a
        warning so operators can track residual usage."""
        import logging

        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=True)
        alias = _tool_by_name(tools, "memory_note")

        with caplog.at_level(logging.WARNING, logger="tools.memory_tools"):
            result = _invoke_with_tool_call(alias, {"text": "hi"}, "c1")

        # Behaviour identical to the canonical tool — observation appended,
        # informative ToolMessage content returned.
        assert result.update["observations"] == ["hi"]
        assert "#1" in result.update["messages"][0].content
        assert "including this call" in result.update["messages"][0].content
        # Exactly one deprecation warning emitted for this call.
        matched = [
            r for r in caplog.records
            if "memory.deprecated_tool_name" in r.getMessage()
        ]
        assert len(matched) == 1

    def test_not_registered_when_disabled(self) -> None:
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=False, auto_write=False)
        names = [t.name for t in tools]
        assert "note_finding" not in names
        assert "memory_note" not in names


# ---------------------------------------------------------------------------
# save_memory (Task 12)
# ---------------------------------------------------------------------------


class TestSaveMemoryArguments:
    def test_accepts_normal_reason(self) -> None:
        parsed = SaveMemoryArguments(
            reason="this run shipped the fix", tool_call_id="call_x"
        )
        assert parsed.reason.startswith("this run")

    def test_rejects_empty_reason(self) -> None:
        with pytest.raises(ValidationError):
            SaveMemoryArguments(reason="", tool_call_id="call_x")

    def test_rejects_over_limit(self) -> None:
        with pytest.raises(ValidationError):
            SaveMemoryArguments(
                reason="a" * (SAVE_MEMORY_REASON_MAX_LEN + 1),
                tool_call_id="call_x",
            )


class TestCommitMemoryTool:
    """Coverage for the canonical ``commit_memory`` tool plus the
    ``save_memory`` alias retained for backward compatibility (issue #102).
    Both route to the same handler; the alias emits a deprecation warning.
    """

    def test_commit_memory_opts_in_and_writes_to_commit_rationales(self) -> None:
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=False)
        tool = _tool_by_name(tools, "commit_memory")

        result = _invoke_with_tool_call(
            tool, {"reason": "shipped the fix"}, "call_commit_1"
        )
        assert isinstance(result, Command)
        assert result.update["memory_opt_in"] is True
        # Issue #102 — rationale lands on its own channel, NOT mixed into
        # observations anymore.  Observations is not touched by this tool.
        assert result.update["commit_rationales"] == ["shipped the fix"]
        assert "observations" not in result.update
        messages = result.update["messages"]
        assert len(messages) == 1
        assert messages[0].tool_call_id == "call_commit_1"
        # Informative return — tells the agent the opt-in landed and reassures
        # that an empty-findings commit still produces a useful memory entry
        # (composed from transcript + rationale). Issue #102.
        assert "Commit confirmed" in messages[0].content
        assert "No findings captured" in messages[0].content

    def test_return_counts_findings_from_observations_only(self) -> None:
        """The count in commit_memory's return is simply
        ``len(observations)`` — rationales no longer pollute observations so
        the old ``[save_memory]`` filter is unnecessary."""
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=False)
        tool = _tool_by_name(tools, "commit_memory")

        result = _invoke_with_tool_call(
            tool,
            {"reason": "another reason"},
            observations=["real finding 1", "real finding 2", "real finding 3"],
        )
        assert "3 finding" in result.update["messages"][0].content

    def test_save_memory_alias_logs_deprecation(self, caplog) -> None:
        """The deprecated ``save_memory`` alias routes to the same handler
        but emits a warning so operators can track residual usage. Mirrors
        the ``memory_note`` alias coverage in ``TestMemoryNoteTool``."""
        import logging

        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=False)
        alias = _tool_by_name(tools, "save_memory")

        with caplog.at_level(logging.WARNING, logger="tools.memory_tools"):
            result = _invoke_with_tool_call(
                alias, {"reason": "shipped"}, "call_save_1"
            )

        assert result.update["memory_opt_in"] is True
        assert result.update["commit_rationales"] == ["shipped"]
        matched = [
            r for r in caplog.records
            if "memory.deprecated_tool_name" in r.getMessage()
            and "used=save_memory" in r.getMessage()
        ]
        assert len(matched) == 1

    def test_strips_whitespace_around_reason(self) -> None:
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=False)
        tool = _tool_by_name(tools, "commit_memory")

        result = _invoke_with_tool_call(tool, {"reason": "   trimmed    "})
        assert result.update["commit_rationales"] == ["trimmed"]

    def test_whitespace_only_reason_raises_tool_error(self) -> None:
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=False)
        tool = _tool_by_name(tools, "commit_memory")

        with pytest.raises(MemoryToolError):
            _invoke_with_tool_call(tool, {"reason": "     "})

    def test_not_registered_in_always_mode(self) -> None:
        """Neither ``commit_memory`` nor the ``save_memory`` alias is
        necessary when the run will write unconditionally — keep the tool
        list lean."""
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=True)
        names = [t.name for t in tools]
        assert "commit_memory" not in names
        assert "save_memory" not in names

    def test_not_registered_in_skip_or_memory_disabled(self) -> None:
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=False, auto_write=False)
        names = [t.name for t in tools]
        assert "commit_memory" not in names
        assert "save_memory" not in names


# Keep the legacy class name as a back-compat alias for any external test
# runners that filter by class name; the tests live on the renamed class
# above. Remove when the ``save_memory`` alias tool is retired.
TestSaveMemoryTool = TestCommitMemoryTool


# ---------------------------------------------------------------------------
# memory_search
# ---------------------------------------------------------------------------


class TestMemorySearchArguments:
    def test_default_mode_and_limit(self) -> None:
        parsed = MemorySearchArguments(query="user onboarding")
        assert parsed.mode == "hybrid"
        assert parsed.limit == 5

    def test_limit_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            MemorySearchArguments(query="x", limit=MEMORY_SEARCH_TOOL_LIMIT_MAX + 1)

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MemorySearchArguments(query="")


class TestMemorySearchTool:
    @pytest.mark.asyncio
    async def test_hybrid_happy_path_delegates_with_bound_agent(self) -> None:
        payload = {
            "results": [
                {
                    "memory_id": "mem-1",
                    "title": "Past task",
                    "summary_preview": "did X",
                    "outcome": "succeeded",
                    "task_id": "t-old",
                    "created_at": "2026-04-01T00:00:00Z",
                    "score": 0.9,
                }
            ],
            "ranking_used": "hybrid",
        }
        http = _FakeHttpClient([_FakeHttpResponse(status_code=200, json_payload=payload)])
        ctx = _make_ctx(agent_id="agent-A", http_client=http)
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=True)
        tool = _tool_by_name(tools, "memory_search")

        result = await tool.ainvoke({"query": "onboarding", "mode": "hybrid", "limit": 5})

        # URL contains the BOUND agent_id, not anything from the LLM.
        assert http.get_calls[0]["url"] == "http://api.internal:8080/v1/agents/agent-A/memory/search"
        assert http.get_calls[0]["params"] == {"q": "onboarding", "mode": "hybrid", "limit": "5"}
        assert result["results"][0]["memory_id"] == "mem-1"
        assert result["ranking_used"] == "hybrid"

    @pytest.mark.asyncio
    async def test_hybrid_silent_degrade_passes_through_ranking_used(self) -> None:
        payload = {"results": [], "ranking_used": "text"}
        http = _FakeHttpClient([_FakeHttpResponse(status_code=200, json_payload=payload)])
        ctx = _make_ctx(http_client=http)
        tool = _tool_by_name(
            build_memory_tools(ctx, stack_enabled=True, auto_write=True), "memory_search"
        )
        result = await tool.ainvoke({"query": "x"})
        assert result["ranking_used"] == "text"

    @pytest.mark.asyncio
    async def test_vector_503_raises_recoverable_tool_error(self) -> None:
        http = _FakeHttpClient([_FakeHttpResponse(status_code=503)])
        ctx = _make_ctx(http_client=http)
        tool = _tool_by_name(
            build_memory_tools(ctx, stack_enabled=True, auto_write=True), "memory_search"
        )
        with pytest.raises(MemorySearchVectorUnavailableError) as excinfo:
            await tool.ainvoke({"query": "x", "mode": "vector"})
        assert "mode='text'" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_404_returns_empty_results_shape(self) -> None:
        http = _FakeHttpClient([_FakeHttpResponse(status_code=404)])
        ctx = _make_ctx(http_client=http)
        tool = _tool_by_name(
            build_memory_tools(ctx, stack_enabled=True, auto_write=True), "memory_search"
        )
        result = await tool.ainvoke({"query": "x", "mode": "hybrid"})
        assert result == {"results": [], "ranking_used": "hybrid"}

    @pytest.mark.asyncio
    async def test_transport_error_surfaces_tool_error(self) -> None:
        http = _FakeHttpClient(httpx.ConnectError("unreachable"))
        ctx = _make_ctx(http_client=http)
        tool = _tool_by_name(
            build_memory_tools(ctx, stack_enabled=True, auto_write=True), "memory_search"
        )
        with pytest.raises(MemoryToolError):
            await tool.ainvoke({"query": "x"})

    @pytest.mark.asyncio
    async def test_crafted_query_cannot_broaden_agent_scope(self) -> None:
        """An attacker-shaped query must not change the URL's agent_id."""
        payload = {"results": [], "ranking_used": "hybrid"}
        http = _FakeHttpClient([_FakeHttpResponse(status_code=200, json_payload=payload)])
        ctx = _make_ctx(agent_id="agent-A", http_client=http)
        tool = _tool_by_name(
            build_memory_tools(ctx, stack_enabled=True, auto_write=True), "memory_search"
        )
        await tool.ainvoke(
            {
                "query": "/../../agents/agent-B/memory/search?agent_id=agent-B",
                "mode": "hybrid",
            }
        )
        # The URL path still points at the bound agent-A.
        assert http.get_calls[0]["url"].endswith("/v1/agents/agent-A/memory/search")


# ---------------------------------------------------------------------------
# task_history_get
# ---------------------------------------------------------------------------


class TestTaskHistoryGetArguments:
    def test_accepts_uuid_shaped_string(self) -> None:
        parsed = TaskHistoryGetArguments(task_id="11111111-1111-1111-1111-111111111111")
        assert parsed.task_id.startswith("1111")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            TaskHistoryGetArguments(task_id="")


class TestTaskHistoryGetTool:
    @pytest.mark.asyncio
    async def test_scope_bound_query_returns_structured_view(self) -> None:
        created_at = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
        row = {
            "task_id": "22222222-2222-2222-2222-222222222222",
            "agent_id": "agent-A",
            "input": "Investigate X",
            "status": "completed",
            "output": {"response": "All done."},
            "last_error_code": None,
            "last_error_message": None,
            "created_at": created_at,
            "memory_id": "mem-1",
        }
        pool = _FakePool(fetchrow_return=row)
        ctx = _make_ctx(tenant_id="default", agent_id="agent-A", pool=pool)
        tool = _tool_by_name(
            build_memory_tools(ctx, stack_enabled=False, auto_write=False),
            "task_history_get",
        )

        result = await tool.ainvoke(
            {"task_id": "22222222-2222-2222-2222-222222222222"}
        )

        # Both scope predicates appear in the SQL + args (last two args are
        # tenant_id, agent_id).
        assert len(pool.calls) == 1
        sql = pool.calls[0][0]
        assert "tenant_id = $2" in sql
        assert "agent_id = $3" in sql
        # Args are (sql, task_id, tenant_id, agent_id)
        assert pool.calls[0][2] == "default"
        assert pool.calls[0][3] == "agent-A"

        assert result["task_id"] == row["task_id"]
        assert result["agent_id"] == "agent-A"
        assert result["input"] == "Investigate X"
        assert result["status"] == "completed"
        assert result["final_output"] == "All done."
        # Without a checkpointer on the context the reader degrades to [].
        assert result["tool_calls"] == []
        assert result["memory_id"] == "mem-1"
        assert result["created_at"].startswith("2026-04-01")

    @pytest.mark.asyncio
    async def test_populates_tool_calls_from_checkpointer(self) -> None:
        """When the context carries a checkpointer, ``tool_calls`` is filled
        from the target task's message history. Verifies the tool→reader
        wiring end to end (with a stub checkpointer, not a real DB)."""
        created_at = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
        row = {
            "task_id": "22222222-2222-2222-2222-222222222222",
            "agent_id": "agent-A",
            "input": "Investigate X",
            "status": "completed",
            "output": {"response": "All done."},
            "last_error_code": None,
            "last_error_message": None,
            "created_at": created_at,
            "memory_id": "mem-1",
        }

        class _StubAIMessage:
            def __init__(self, tool_calls):
                self.tool_calls = tool_calls
                self.content = None

        class _StubToolMessage:
            def __init__(self, tool_call_id, content):
                self.tool_call_id = tool_call_id
                self.content = content

        class _StubTuple:
            def __init__(self, checkpoint):
                self.checkpoint = checkpoint

        class _StubCheckpointer:
            def __init__(self, messages):
                self._messages = messages
                self.calls = []

            async def aget_tuple(self, config):
                self.calls.append(config)
                return _StubTuple({"channel_values": {"messages": self._messages}})

        ai = _StubAIMessage(tool_calls=[
            {"id": "c-1", "name": "web_search", "args": {"q": "cache bug"}},
        ])
        tool_result = _StubToolMessage("c-1", "Found 3 matches")
        checkpointer = _StubCheckpointer([ai, tool_result])

        pool = _FakePool(fetchrow_return=row)
        ctx = _make_ctx(
            tenant_id="default",
            agent_id="agent-A",
            pool=pool,
            checkpointer=checkpointer,
        )
        tool = _tool_by_name(
            build_memory_tools(ctx, stack_enabled=False, auto_write=False),
            "task_history_get",
        )

        result = await tool.ainvoke(
            {"task_id": "22222222-2222-2222-2222-222222222222"}
        )

        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == "web_search"
        assert result["tool_calls"][0]["result_preview"] == "Found 3 matches"
        # Reader was invoked with the queried task_id as thread_id.
        assert checkpointer.calls == [
            {"configurable": {"thread_id": "22222222-2222-2222-2222-222222222222"}}
        ]

    @pytest.mark.asyncio
    async def test_cross_agent_miss_returns_not_found(self) -> None:
        # Scope-miss: repo returns None when the task exists under a different
        # agent_id. Tool translates that into a uniform "not found".
        pool = _FakePool(fetchrow_return=None)
        ctx = _make_ctx(agent_id="agent-A", pool=pool)
        tool = _tool_by_name(
            build_memory_tools(ctx, stack_enabled=False, auto_write=False),
            "task_history_get",
        )
        with pytest.raises(MemoryToolNotFoundError):
            await tool.ainvoke({"task_id": "33333333-3333-3333-3333-333333333333"})

    @pytest.mark.asyncio
    async def test_cross_tenant_miss_returns_not_found(self) -> None:
        # Same shape as cross-agent: uniform 404-not-403 at the tool surface.
        pool = _FakePool(fetchrow_return=None)
        ctx = _make_ctx(tenant_id="tenant-A", agent_id="agent-A", pool=pool)
        tool = _tool_by_name(
            build_memory_tools(ctx, stack_enabled=False, auto_write=False),
            "task_history_get",
        )
        with pytest.raises(MemoryToolNotFoundError):
            await tool.ainvoke({"task_id": "44444444-4444-4444-4444-444444444444"})

    @pytest.mark.asyncio
    async def test_malformed_uuid_returns_not_found(self) -> None:
        pool = _FakePool(
            fetchrow_raises=asyncpg.exceptions.DataError("invalid uuid"),
        )
        ctx = _make_ctx(pool=pool)
        tool = _tool_by_name(
            build_memory_tools(ctx, stack_enabled=False, auto_write=False),
            "task_history_get",
        )
        with pytest.raises(MemoryToolNotFoundError):
            await tool.ainvoke({"task_id": "not-a-uuid"})

    @pytest.mark.asyncio
    async def test_large_input_output_truncated(self) -> None:
        big = "a" * 5000
        row = {
            "task_id": "55555555-5555-5555-5555-555555555555",
            "agent_id": "agent-A",
            "input": big,
            "status": "completed",
            "output": {"response": big},
            "last_error_code": None,
            "last_error_message": None,
            "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "memory_id": None,
        }
        pool = _FakePool(fetchrow_return=row)
        ctx = _make_ctx(pool=pool)
        tool = _tool_by_name(
            build_memory_tools(ctx, stack_enabled=False, auto_write=False),
            "task_history_get",
        )
        result = await tool.ainvoke(
            {"task_id": "55555555-5555-5555-5555-555555555555"}
        )
        assert len(result["input"].encode("utf-8")) <= 2048 + len("...[truncated]")
        assert result["input"].endswith("[truncated]")
        assert len(result["final_output"].encode("utf-8")) <= 2048 + len("...[truncated]")


# ---------------------------------------------------------------------------
# build_memory_tools — gating + tool-count expectations
# ---------------------------------------------------------------------------


class TestBuildMemoryToolsGating:
    def test_always_mode_registers_note_finding_alias_search_and_history(self) -> None:
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=True)
        names = sorted(t.name for t in tools)
        # ``save_memory`` is NOT registered in ``always`` mode — the run
        # writes unconditionally, so the tool would be a no-op.
        # ``memory_note`` alias is registered alongside ``note_finding``
        # for backward compat with in-flight checkpoints (issue #102).
        assert names == [
            "memory_note",
            "memory_search",
            "note_finding",
            "task_history_get",
        ]

    def test_agent_decides_mode_also_registers_commit_memory(self) -> None:
        """``agent_decides`` mode registers both the canonical
        ``commit_memory`` tool AND the legacy ``save_memory`` alias for
        backward compat with in-flight checkpoints (issue #102)."""
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=True, auto_write=False)
        names = sorted(t.name for t in tools)
        assert names == [
            "commit_memory",
            "memory_note",
            "memory_search",
            "note_finding",
            "save_memory",
            "task_history_get",
        ]

    def test_skip_mode_registers_only_task_history_get(self) -> None:
        ctx = _make_ctx()
        tools = build_memory_tools(ctx, stack_enabled=False, auto_write=False)
        names = [t.name for t in tools]
        assert names == ["task_history_get"]
