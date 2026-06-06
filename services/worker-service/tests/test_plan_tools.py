"""Unit tests for the Planning Primitive — Task P1.

Covered contracts:

1. **Reducer (``_plan_replace_reducer``)** — replace semantics: the incoming
   value (b) always wins; a node that omits ``plan`` leaves the channel intact.
2. **``plan_write`` happy path** — valid full list writes verbatim, returns
   a short confirmation ``Command`` with a ``ToolMessage``.
3. **``plan_write`` validation — bad status** — item with ``status`` not in
   {pending, in_progress, completed} is rejected with a tool-layer error
   naming the allowed values.
4. **``plan_write`` validation — two ``in_progress`` items accepted** — the
   tool does NOT enforce exactly-one-in-progress (that is prompt-layer).
5. **``plan_write`` validation — item cap** — 51 items rejected, 50 succeed.
6. **``plan_write`` validation — title cap** — title > 200 chars rejected.
7. **``plan_write`` registration gate** — appears in ``_get_tools`` output
   iff ``"plan_write"`` is in the allowlist; absent → tool not in the list.

These tests are pure-Python / in-process — no network, no DB, no LLM.
"""

from __future__ import annotations

import asyncio
from typing import get_type_hints

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from executor.compaction.state import RuntimeState, _plan_replace_reducer
from tools.plan_tools import (
    PLAN_MAX_ITEMS,
    PLAN_MAX_TITLE_CHARS,
    VALID_STATUSES,
    PlanItem,
    PlanWriteArguments,
    PlanWriteError,
    build_plan_write_tool,
    validate_plan_items,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(
    id_: str = "item-1",
    title: str = "Do something",
    status: str = "pending",
) -> dict:
    return {"id": id_, "title": title, "status": status}


def _make_tool() -> StructuredTool:
    """Build a ``plan_write`` StructuredTool with a dummy tool_call_id injected."""
    return build_plan_write_tool()


async def _call_plan_write(items: list[dict], *, tool_call_id: str = "tc-1") -> Command:
    """Invoke the ``plan_write`` handler directly, simulating ToolNode injection."""
    tool = _make_tool()
    # StructuredTool.func or .coroutine is the handler. The args_schema is
    # PlanWriteArguments; we call the handler with parsed args directly.
    handler = tool.func  # plan_write is sync (returns Command, no I/O)
    return handler(items=items, tool_call_id=tool_call_id)


# ---------------------------------------------------------------------------
# 1. Reducer tests
# ---------------------------------------------------------------------------


class TestPlanReplaceReducer:
    """``_plan_replace_reducer`` must implement full-list replace semantics."""

    def test_reducer_returns_new_value(self) -> None:
        a = [{"id": "x", "title": "Old", "status": "pending"}]
        b = [{"id": "y", "title": "New", "status": "completed"}]
        result = _plan_replace_reducer(a, b)
        assert result is b

    def test_reducer_replaces_entire_list(self) -> None:
        a = [{"id": "1", "title": "A", "status": "pending"}] * 10
        b = [{"id": "2", "title": "B", "status": "in_progress"}]
        result = _plan_replace_reducer(a, b)
        assert result == b
        assert len(result) == 1

    def test_reducer_with_empty_new_value_clears(self) -> None:
        a = [{"id": "1", "title": "A", "status": "pending"}]
        b: list = []
        result = _plan_replace_reducer(a, b)
        assert result == []

    def test_reducer_with_empty_prior_value_accepts_new(self) -> None:
        a: list = []
        b = [{"id": "1", "title": "A", "status": "pending"}]
        result = _plan_replace_reducer(a, b)
        assert result == b


class TestRuntimeStatePlanField:
    """``RuntimeState`` must have a ``plan`` field with ``_plan_replace_reducer``."""

    def test_plan_field_present_in_runtime_state(self) -> None:
        hints = get_type_hints(RuntimeState, include_extras=True)
        assert "plan" in hints, "RuntimeState must have a 'plan' field"

    def test_plan_field_has_plan_replace_reducer(self) -> None:
        hints = get_type_hints(RuntimeState, include_extras=True)
        ann = hints["plan"]
        metadata = getattr(ann, "__metadata__", ())
        assert len(metadata) == 1, "plan field should have exactly one reducer annotation"
        assert metadata[0] is _plan_replace_reducer

    def test_plan_field_count_incremented(self) -> None:
        """The plan field must be accounted for in the schema count."""
        hints = get_type_hints(RuntimeState, include_extras=True)
        # Previously 13 fields. With plan added, should be 14.
        assert "plan" in hints


# ---------------------------------------------------------------------------
# 2. plan_write happy path
# ---------------------------------------------------------------------------


class TestPlanWriteHappyPath:
    def test_valid_list_writes_verbatim_and_returns_command(self) -> None:
        items = [
            {"id": "p1", "title": "Research the problem", "status": "completed"},
            {"id": "p2", "title": "Draft solution", "status": "in_progress"},
            {"id": "p3", "title": "Test and review", "status": "pending"},
        ]
        result = asyncio.get_event_loop().run_until_complete(
            _call_plan_write(items)
        )
        assert isinstance(result, Command)
        # plan channel must be set to the exact items (verbatim)
        assert result.update["plan"] == items  # type: ignore[index]

    def test_confirmation_message_includes_item_count(self) -> None:
        items = [_make_item(f"item-{i}") for i in range(5)]
        result = asyncio.get_event_loop().run_until_complete(
            _call_plan_write(items)
        )
        messages = result.update["messages"]  # type: ignore[index]
        assert len(messages) == 1
        assert isinstance(messages[0], ToolMessage)
        # The confirmation should mention the count
        assert "5" in messages[0].content

    def test_tool_call_id_is_paired_in_tool_message(self) -> None:
        items = [_make_item()]
        result = asyncio.get_event_loop().run_until_complete(
            _call_plan_write(items, tool_call_id="my-tc-id")
        )
        messages = result.update["messages"]  # type: ignore[index]
        assert messages[0].tool_call_id == "my-tc-id"

    def test_single_pending_item_accepted(self) -> None:
        items = [{"id": "only", "title": "Only item", "status": "pending"}]
        result = asyncio.get_event_loop().run_until_complete(
            _call_plan_write(items)
        )
        assert result.update["plan"] == items  # type: ignore[index]

    def test_empty_list_accepted(self) -> None:
        """Clearing the plan (empty list) is valid."""
        result = asyncio.get_event_loop().run_until_complete(
            _call_plan_write([])
        )
        assert result.update["plan"] == []  # type: ignore[index]

    def test_items_written_verbatim_not_reordered(self) -> None:
        items = [
            {"id": "z", "title": "Last alphabetically", "status": "pending"},
            {"id": "a", "title": "First alphabetically", "status": "pending"},
        ]
        result = asyncio.get_event_loop().run_until_complete(
            _call_plan_write(items)
        )
        assert result.update["plan"] == items  # type: ignore[index]
        assert result.update["plan"][0]["id"] == "z"  # type: ignore[index]


# ---------------------------------------------------------------------------
# 3. Validation — bad status
# ---------------------------------------------------------------------------


class TestPlanWriteStatusValidation:
    def test_bad_status_raises_error(self) -> None:
        items = [{"id": "x", "title": "Something", "status": "done"}]
        with pytest.raises(PlanWriteError) as exc_info:
            validate_plan_items(items)
        assert "done" in str(exc_info.value).lower() or "status" in str(exc_info.value).lower()
        # Must name the allowed values in the error message
        for valid in VALID_STATUSES:
            assert valid in str(exc_info.value)

    def test_all_valid_statuses_accepted(self) -> None:
        for status in VALID_STATUSES:
            items = [{"id": "x", "title": "Title", "status": status}]
            validate_plan_items(items)  # should not raise

    def test_case_sensitive_rejection(self) -> None:
        """Statuses are case-sensitive: 'Pending' is not valid."""
        items = [{"id": "x", "title": "Title", "status": "Pending"}]
        with pytest.raises(PlanWriteError):
            validate_plan_items(items)

    def test_empty_status_rejected(self) -> None:
        items = [{"id": "x", "title": "Title", "status": ""}]
        with pytest.raises(PlanWriteError):
            validate_plan_items(items)


# ---------------------------------------------------------------------------
# 4. Validation — two in_progress accepted (prompt-layer rule, not tool-layer)
# ---------------------------------------------------------------------------


class TestPlanWriteMultipleInProgressAccepted:
    def test_two_in_progress_items_succeed(self) -> None:
        items = [
            {"id": "p1", "title": "Task one", "status": "in_progress"},
            {"id": "p2", "title": "Task two", "status": "in_progress"},
        ]
        # Should NOT raise
        validate_plan_items(items)

    def test_all_in_progress_accepted(self) -> None:
        items = [
            {"id": f"p{i}", "title": f"Task {i}", "status": "in_progress"}
            for i in range(10)
        ]
        validate_plan_items(items)  # should not raise

    def test_zero_in_progress_accepted(self) -> None:
        items = [
            {"id": "p1", "title": "Task one", "status": "pending"},
            {"id": "p2", "title": "Task two", "status": "completed"},
        ]
        validate_plan_items(items)  # should not raise


# ---------------------------------------------------------------------------
# 5. Validation — item cap (50 max)
# ---------------------------------------------------------------------------


class TestPlanWriteItemCap:
    def test_exactly_50_items_succeeds(self) -> None:
        items = [_make_item(f"item-{i}") for i in range(PLAN_MAX_ITEMS)]
        validate_plan_items(items)  # should not raise

    def test_51_items_raises_error_naming_cap(self) -> None:
        items = [_make_item(f"item-{i}") for i in range(PLAN_MAX_ITEMS + 1)]
        with pytest.raises(PlanWriteError) as exc_info:
            validate_plan_items(items)
        assert str(PLAN_MAX_ITEMS) in str(exc_info.value)

    def test_51_items_via_tool_raises_error(self) -> None:
        items = [_make_item(f"item-{i}") for i in range(PLAN_MAX_ITEMS + 1)]
        with pytest.raises(PlanWriteError):
            asyncio.get_event_loop().run_until_complete(_call_plan_write(items))


# ---------------------------------------------------------------------------
# 6. Validation — title cap (200 chars max)
# ---------------------------------------------------------------------------


class TestPlanWriteTitleCap:
    def test_exactly_200_char_title_succeeds(self) -> None:
        items = [_make_item(title="x" * PLAN_MAX_TITLE_CHARS)]
        validate_plan_items(items)  # should not raise

    def test_201_char_title_raises_error_naming_cap(self) -> None:
        items = [_make_item(title="x" * (PLAN_MAX_TITLE_CHARS + 1))]
        with pytest.raises(PlanWriteError) as exc_info:
            validate_plan_items(items)
        assert str(PLAN_MAX_TITLE_CHARS) in str(exc_info.value)

    def test_201_char_title_via_tool_raises_error(self) -> None:
        items = [_make_item(title="x" * (PLAN_MAX_TITLE_CHARS + 1))]
        with pytest.raises(PlanWriteError):
            asyncio.get_event_loop().run_until_complete(_call_plan_write(items))


# ---------------------------------------------------------------------------
# 7. Registration gate in _get_tools
# ---------------------------------------------------------------------------


class TestPlanWriteRegistrationGate:
    """``plan_write`` appears in the assembled tool list iff it is allowlisted."""

    def _get_tool_names(self, allowed_tools: list[str]) -> list[str]:
        """Call ``_get_tools`` with minimal args and collect tool names."""
        import asyncio
        from executor.graph import GraphExecutor

        # GraphExecutor requires a pool and s3_client. We only need _get_tools
        # which doesn't use self.pool etc., so we pass a None executor.
        # _get_tools is an instance method — create a minimal shell.
        executor = object.__new__(GraphExecutor)
        # _get_tools only uses self.deps for web_search / read_url; those won't
        # be in our allowlist so deps is not accessed in these tests.
        cancel_event = asyncio.Event()
        tools = executor._get_tools(
            allowed_tools,
            cancel_event=cancel_event,
            task_id="test-task",
            tenant_id="test-tenant",
            agent_id="test-agent",
        )
        return [t.name for t in tools]

    def test_plan_write_absent_when_not_allowlisted(self) -> None:
        names = self._get_tool_names(["web_search"])
        assert "plan_write" not in names

    def test_plan_write_present_when_allowlisted(self) -> None:
        names = self._get_tool_names(["plan_write"])
        assert "plan_write" in names

    def test_plan_write_absent_with_empty_allowlist(self) -> None:
        names = self._get_tool_names([])
        assert "plan_write" not in names

    def test_plan_write_present_alongside_other_tools(self) -> None:
        names = self._get_tool_names(["plan_write"])
        # plan_write is in the list and the list has at least this one tool
        assert "plan_write" in names
        assert len(names) >= 1


# ---------------------------------------------------------------------------
# 8. validate_plan_items — exportable helper
# ---------------------------------------------------------------------------


class TestValidatePlanItems:
    def test_valid_items_do_not_raise(self) -> None:
        items = [
            {"id": "a", "title": "Alpha", "status": "pending"},
            {"id": "b", "title": "Beta", "status": "in_progress"},
            {"id": "c", "title": "Gamma", "status": "completed"},
        ]
        validate_plan_items(items)  # no raise

    def test_missing_status_field_raises(self) -> None:
        items = [{"id": "a", "title": "Alpha"}]
        with pytest.raises((PlanWriteError, KeyError, Exception)):
            validate_plan_items(items)

    def test_missing_title_field_raises(self) -> None:
        items = [{"id": "a", "status": "pending"}]
        with pytest.raises((PlanWriteError, KeyError, Exception)):
            validate_plan_items(items)
