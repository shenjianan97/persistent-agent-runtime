"""Unit tests for executor.compaction.summarizer — Task 7 Tier 3 Summarizer.

All tests run offline without provider credentials. The LangChain
``init_chat_model`` call is patched in every scenario that would touch
a live LLM. The cost-ledger is provided as a minimal async mock so tests
stay isolated from the database.

Test coverage:
- Happy path: successful summarisation + ledger row
- Empty-slice guard: < 2 messages returns skipped=True, skipped_reason="empty_slice"
- Single-message guard: 1 message also returns skipped
- Retry-then-success: transient error on first attempt, success on second
- Retry exhaustion: all attempts fail with retryable error → skipped_reason="retryable"
- Fatal error: non-retryable error → skipped_reason="fatal"
- Cost-ledger row written: attributes match expected shape
- Model-override honoured: caller-supplied model_id is used, not default
- Langfuse callbacks propagated: callback list forwarded to ainvoke
- format_messages_for_summary determinism: two calls on same slice are byte-equal
- format_messages_for_summary structure: each message type renders correctly
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from executor.compaction.summarizer import (
    SummarizeResult,
    format_messages_for_summary,
    summarize_slice,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ai_message_with_tool_call(content: str = "thinking...", tool_name: str = "do_thing", args: dict | None = None) -> AIMessage:
    """Return an AIMessage with one tool_call entry."""
    return AIMessage(
        content=content,
        tool_calls=[
            {
                "id": "call_abc123",
                "name": tool_name,
                "args": args or {"param": "value"},
            }
        ],
    )


def _make_tool_message(content: str = "tool result", tool_call_id: str = "call_abc123", name: str = "do_thing") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id, name=name)


def _make_fake_llm_response(content: str = "Summary of prior context.") -> MagicMock:
    """Build a fake LLM response object with usage metadata."""
    resp = MagicMock()
    resp.content = content
    resp.response_metadata = {
        "usage": {
            "input_tokens": 120,
            "output_tokens": 40,
        }
    }
    resp.usage_metadata = None
    return resp


@dataclass
class _LedgerRow:
    """Records one call to insert_cost_row for inspection."""
    tenant_id: str
    agent_id: str
    task_id: str
    checkpoint_id: str | None
    cost_microdollars: int
    operation: str
    model_id: str | None
    tokens_in: int
    tokens_out: int
    summarized_through_turn_index_after: int | None


class _FakeCostLedger:
    """In-memory stand-in for CostLedgerRepository."""

    def __init__(self) -> None:
        self.rows: list[_LedgerRow] = []

    async def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        task_id: str,
        checkpoint_id: str | None,
        cost_microdollars: int,
        operation: str,
        model_id: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        summarized_through_turn_index_after: int | None = None,
    ) -> None:
        self.rows.append(
            _LedgerRow(
                tenant_id=tenant_id,
                agent_id=agent_id,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                cost_microdollars=cost_microdollars,
                operation=operation,
                model_id=model_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                summarized_through_turn_index_after=summarized_through_turn_index_after,
            )
        )


# ---------------------------------------------------------------------------
# Shared test parameters
# ---------------------------------------------------------------------------

TASK_ID = "task-aaa-111"
TENANT_ID = "tenant-xyz"
AGENT_ID = "agent-007"
CHECKPOINT_ID = "chk-bbb-222"
MODEL_ID = "claude-haiku-4-5"

# A minimal slice with at least 2 messages so the empty-slice guard doesn't fire.
def _two_message_slice() -> list[BaseMessage]:
    return [
        HumanMessage(content="Please read the file"),
        _make_tool_message("file content here"),
    ]


def _rich_slice() -> list[BaseMessage]:
    return [
        SystemMessage(content="You are a helpful agent."),
        HumanMessage(content="Start the task"),
        _make_ai_message_with_tool_call("I will read the file", "read_file", {"path": "/tmp/data.txt"}),
        _make_tool_message("contents of /tmp/data.txt", "call_abc123", "read_file"),
    ]


# ---------------------------------------------------------------------------
# format_messages_for_summary — determinism + structure tests
# ---------------------------------------------------------------------------


def test_format_messages_deterministic():
    """Two calls on identical slice produce byte-equal output."""
    slice_msgs = _rich_slice()
    result_a = format_messages_for_summary(slice_msgs)
    result_b = format_messages_for_summary(slice_msgs)
    assert result_a == result_b


def test_format_messages_system_prefix():
    """SystemMessage renders as 'SYSTEM: ...'."""
    msgs = [SystemMessage(content="sys prompt"), HumanMessage(content="hi")]
    out = format_messages_for_summary(msgs)
    assert "SYSTEM: sys prompt" in out


def test_format_messages_human_prefix():
    """HumanMessage renders as 'USER: ...'."""
    msgs = [HumanMessage(content="hello world"), HumanMessage(content="second")]
    out = format_messages_for_summary(msgs)
    assert "USER: hello world" in out


def test_format_messages_ai_prefix_with_tool_calls():
    """AIMessage with tool_calls renders with step index and tool call names."""
    ai_msg = _make_ai_message_with_tool_call("I'll call the tool", "my_tool", {"key": "val"})
    msgs = [HumanMessage(content="go"), ai_msg]
    out = format_messages_for_summary(msgs)
    assert "ASSISTANT" in out
    assert "my_tool" in out
    assert "step 1" in out.lower() or "step" in out


def test_format_messages_tool_result_prefix():
    """ToolMessage renders with call_id and name."""
    msgs = [
        _make_tool_message("the result", "call_xyz", "search_web"),
        HumanMessage(content="ok"),
    ]
    out = format_messages_for_summary(msgs)
    assert "TOOL_RESULT" in out
    assert "call_xyz" in out or "search_web" in out


def test_format_messages_ai_args_sorted():
    """JSON args in AIMessage tool_calls are sorted by key (determinism check)."""
    ai_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "c1",
                "name": "tool_a",
                "args": {"z_key": "zval", "a_key": "aval"},
            }
        ],
    )
    msgs = [HumanMessage(content="x"), ai_msg]
    out = format_messages_for_summary(msgs)
    # "a_key" must appear before "z_key" in output (sort_keys=True on json.dumps)
    idx_a = out.find('"a_key"')
    idx_z = out.find('"z_key"')
    assert idx_a != -1 and idx_z != -1
    assert idx_a < idx_z


# ---------------------------------------------------------------------------
# summarize_slice — empty-slice guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_slice_no_messages():
    """Zero-message slice returns skipped=True without calling the LLM."""
    ledger = _FakeCostLedger()
    result = await summarize_slice(
        slice_messages=[],
        summarizer_model_id=MODEL_ID,
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        checkpoint_id=CHECKPOINT_ID,
        cost_ledger=ledger,
    )
    assert result.skipped is True
    assert result.skipped_reason == "empty_slice"
    assert result.summary_text is None
    assert len(ledger.rows) == 0


@pytest.mark.asyncio
async def test_single_message_slice_returns_skipped():
    """Single-message slice (< 2) returns skipped=True without calling the LLM."""
    ledger = _FakeCostLedger()
    result = await summarize_slice(
        slice_messages=[HumanMessage(content="just one")],
        summarizer_model_id=MODEL_ID,
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        checkpoint_id=CHECKPOINT_ID,
        cost_ledger=ledger,
    )
    assert result.skipped is True
    assert result.skipped_reason == "empty_slice"
    assert len(ledger.rows) == 0


# ---------------------------------------------------------------------------
# summarize_slice — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_summary_text():
    """Happy path: mocked LLM returns summary text → result.skipped is False."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_llm_response("Agent read /tmp/data.txt and found 42 lines.")

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=_two_message_slice(),
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
        )

    assert result.skipped is False
    assert result.summary_text == "Agent read /tmp/data.txt and found 42 lines."
    assert result.summarizer_model_id == MODEL_ID


@pytest.mark.asyncio
async def test_happy_path_writes_ledger_row():
    """Happy path: exactly one cost-ledger row with operation='compaction.tier3'."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_llm_response("Summary text.")

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        await summarize_slice(
            slice_messages=_two_message_slice(),
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
        )

    assert len(ledger.rows) == 1
    row = ledger.rows[0]
    assert row.operation == "compaction.tier3"
    assert row.model_id == MODEL_ID
    assert row.tenant_id == TENANT_ID
    assert row.agent_id == AGENT_ID
    assert row.task_id == TASK_ID
    assert row.checkpoint_id == CHECKPOINT_ID


@pytest.mark.asyncio
async def test_happy_path_token_counts():
    """Token counts from LLM response metadata are echoed in the result."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_llm_response("Summary.")
    # usage: input_tokens=120, output_tokens=40 set in _make_fake_llm_response

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=_two_message_slice(),
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
        )

    assert result.tokens_in == 120
    assert result.tokens_out == 40
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_happy_path_model_override():
    """Caller-supplied summarizer_model_id is forwarded to init_chat_model."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_llm_response("Summary.")
    custom_model = "claude-sonnet-4-5"

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=_two_message_slice(),
            summarizer_model_id=custom_model,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
        )

    assert result.summarizer_model_id == custom_model
    assert ledger.rows[0].model_id == custom_model
    # init_chat_model called with the custom model
    call_kwargs = mock_init.call_args
    assert custom_model in call_kwargs.args or call_kwargs.kwargs.get("model") == custom_model


@pytest.mark.asyncio
async def test_happy_path_callbacks_forwarded():
    """Langfuse callbacks list is forwarded to llm.ainvoke."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_llm_response("Summary.")
    fake_callback = MagicMock()

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        await summarize_slice(
            slice_messages=_two_message_slice(),
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
            callbacks=[fake_callback],
        )

    # ainvoke must have been called with callbacks in its config
    ainvoke_call = mock_llm.ainvoke.call_args
    # callbacks appear either as positional config dict or as kwargs
    all_args = list(ainvoke_call.args) + list(ainvoke_call.kwargs.values())
    found = any(
        isinstance(a, dict) and fake_callback in a.get("callbacks", [])
        for a in all_args
    )
    assert found, f"Expected callback in ainvoke call, got: {ainvoke_call}"


# ---------------------------------------------------------------------------
# summarize_slice — retry logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_then_success():
    """First attempt raises a transient error, second attempt succeeds."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_llm_response("Summary after retry.")

    call_count = 0

    async def side_effect(messages, config=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a 503 transient error
            err = Exception("503 Service Unavailable")
            raise err
        return fake_response

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        with patch("executor.compaction.summarizer.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_llm = AsyncMock()
            mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
            mock_init.return_value = mock_llm

            result = await summarize_slice(
                slice_messages=_two_message_slice(),
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
            )

    assert result.skipped is False
    assert result.summary_text == "Summary after retry."
    assert call_count == 2
    assert len(ledger.rows) == 1  # only one ledger row on success


@pytest.mark.asyncio
async def test_retry_exhaustion_returns_skipped_retryable():
    """All attempts fail with a transient error → skipped=True, skipped_reason='retryable'."""
    ledger = _FakeCostLedger()
    transient_error = Exception("502 Bad Gateway")

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        with patch("executor.compaction.summarizer.asyncio.sleep", new_callable=AsyncMock):
            mock_llm = AsyncMock()
            mock_llm.ainvoke = AsyncMock(side_effect=transient_error)
            mock_init.return_value = mock_llm

            result = await summarize_slice(
                slice_messages=_two_message_slice(),
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
            )

    assert result.skipped is True
    assert result.skipped_reason == "retryable"
    assert result.summary_text is None
    assert len(ledger.rows) == 0  # no ledger row on failure


@pytest.mark.asyncio
async def test_retry_exhaustion_attempt_count():
    """SUMMARIZER_MAX_RETRIES=2 means 3 total attempts before giving up."""
    from executor.compaction.defaults import SUMMARIZER_MAX_RETRIES

    ledger = _FakeCostLedger()
    call_count = 0
    transient_error = Exception("503 Service Unavailable")

    async def side_effect(messages, config=None):
        nonlocal call_count
        call_count += 1
        raise transient_error

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        with patch("executor.compaction.summarizer.asyncio.sleep", new_callable=AsyncMock):
            mock_llm = AsyncMock()
            mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
            mock_init.return_value = mock_llm

            await summarize_slice(
                slice_messages=_two_message_slice(),
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
            )

    # SUMMARIZER_MAX_RETRIES retries + 1 initial attempt
    assert call_count == SUMMARIZER_MAX_RETRIES + 1


# ---------------------------------------------------------------------------
# summarize_slice — fatal (non-retryable) error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fatal_error_returns_skipped_fatal():
    """Non-retryable error (e.g. 401 invalid auth) → skipped=True, skipped_reason='fatal'."""
    ledger = _FakeCostLedger()
    fatal_error = Exception("401 Unauthorized - invalid API key")

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        with patch("executor.compaction.summarizer.asyncio.sleep", new_callable=AsyncMock):
            mock_llm = AsyncMock()
            mock_llm.ainvoke = AsyncMock(side_effect=fatal_error)
            mock_init.return_value = mock_llm

            result = await summarize_slice(
                slice_messages=_two_message_slice(),
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
            )

    assert result.skipped is True
    assert result.skipped_reason == "fatal"
    assert result.summary_text is None
    assert len(ledger.rows) == 0  # no ledger row on fatal failure


@pytest.mark.asyncio
async def test_fatal_error_only_one_attempt():
    """Fatal error is NOT retried — only one LLM call attempt."""
    ledger = _FakeCostLedger()
    call_count = 0
    fatal_error = Exception("400 Bad Request - model not found")

    async def side_effect(messages, config=None):
        nonlocal call_count
        call_count += 1
        raise fatal_error

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        with patch("executor.compaction.summarizer.asyncio.sleep", new_callable=AsyncMock):
            mock_llm = AsyncMock()
            mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
            mock_init.return_value = mock_llm

            await summarize_slice(
                slice_messages=_two_message_slice(),
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
            )

    assert call_count == 1  # fatal: no retries


# ---------------------------------------------------------------------------
# summarize_slice — cost-ledger idempotency + field shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_row_has_all_fields():
    """Cost-ledger row has tenant_id, agent_id, task_id, checkpoint_id, model_id,
    tokens_in, tokens_out, cost_microdollars, operation='compaction.tier3'."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_llm_response("summary")

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        await summarize_slice(
            slice_messages=_two_message_slice(),
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
        )

    row = ledger.rows[0]
    assert row.tenant_id == TENANT_ID
    assert row.agent_id == AGENT_ID
    assert row.task_id == TASK_ID
    assert row.checkpoint_id == CHECKPOINT_ID
    assert row.model_id == MODEL_ID
    assert row.tokens_in == 120
    assert row.tokens_out == 40
    assert row.operation == "compaction.tier3"
    assert isinstance(row.cost_microdollars, int)


@pytest.mark.asyncio
async def test_no_ledger_row_on_empty_slice():
    """Empty slice (< 2 messages) never writes a cost-ledger row."""
    ledger = _FakeCostLedger()
    await summarize_slice(
        slice_messages=[],
        summarizer_model_id=MODEL_ID,
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        checkpoint_id=CHECKPOINT_ID,
        cost_ledger=ledger,
    )
    assert len(ledger.rows) == 0


# ---------------------------------------------------------------------------
# SummarizeResult dataclass
# ---------------------------------------------------------------------------


def test_summarize_result_frozen():
    """SummarizeResult is a frozen dataclass — mutating raises."""
    r = SummarizeResult(
        summary_text="text",
        skipped=False,
        skipped_reason=None,
        summarizer_model_id=MODEL_ID,
        tokens_in=10,
        tokens_out=5,
        cost_microdollars=100,
        latency_ms=250,
    )
    import dataclasses
    assert dataclasses.is_dataclass(r)
    with pytest.raises((AttributeError, TypeError)):
        r.summary_text = "mutated"  # type: ignore[misc]


def test_summarize_result_skipped_has_no_text():
    """Convenience: a skipped result has summary_text=None."""
    r = SummarizeResult(
        summary_text=None,
        skipped=True,
        skipped_reason="retryable",
        summarizer_model_id=MODEL_ID,
        tokens_in=0,
        tokens_out=0,
        cost_microdollars=0,
        latency_ms=0,
    )
    assert r.summary_text is None
    assert r.skipped is True
