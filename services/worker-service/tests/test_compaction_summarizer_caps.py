"""Unit tests for Task 1 of Track 7 Follow-up — summarizer prompt hardening,
``max_tokens`` safety-net, and ``compaction.tier3_output_truncated`` telemetry.

These tests complement ``tests/test_compaction_summarizer.py`` and target only
the behaviours introduced by the Track 7 Follow-up Task 1 contract:

1. ``SUMMARIZER_MAX_OUTPUT_TOKENS = 1500`` is the default cap and is exported
   from ``executor.compaction.defaults``.
2. ``summarize_slice`` forwards ``max_tokens=SUMMARIZER_MAX_OUTPUT_TOKENS`` to
   the underlying ``llm.ainvoke`` call.
3. When the LLM response indicates a truncation-at-cap (finish/stop reason is
   ``"length"`` — OpenAI / Bedrock Converse — or ``"max_tokens"`` — Anthropic),
   the summarizer emits a single ``compaction.tier3_output_truncated`` WARN
   structured log with ``tenant_id``, ``agent_id``, ``task_id``, and
   ``tokens_out`` fields.
4. When the response finishes normally (``finish_reason="stop"`` /
   ``stop_reason="end_turn"``), no truncation WARN is emitted.
5. The platform-owned ``SUMMARIZER_PROMPT`` expresses its output budget in
   *tokens*, warns the model that over-budget output will be truncated with
   the tail lost, and contains a concrete example — these are the prompt
   properties the contract requires so a compliant model treats the budget
   as binding.
6. The tightened prompt preserves the existing preservation-priority list
   (files, URLs, decisions, errors, identifiers) — no behavioural regression.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from executor.compaction.defaults import SUMMARIZER_MAX_OUTPUT_TOKENS
from executor.compaction.summarizer import (
    SUMMARIZER_PROMPT,
    summarize_slice,
)


# ---------------------------------------------------------------------------
# Shared fixtures (mirrors test_compaction_summarizer.py shape)
# ---------------------------------------------------------------------------

TASK_ID = "task-cap-111"
TENANT_ID = "tenant-cap"
AGENT_ID = "agent-cap"
CHECKPOINT_ID = "chk-cap-222"
MODEL_ID = "claude-haiku-4-5"


@dataclass
class _LedgerRow:
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


def _two_message_slice() -> list[BaseMessage]:
    return [
        HumanMessage(content="please read the file"),
        ToolMessage(content="file bytes", tool_call_id="call_abc", name="read_file"),
    ]


def _make_fake_response(
    *,
    content: str = "a synthetic summary",
    finish_reason: str | None = "stop",
    stop_reason: str | None = None,
    tokens_out: int = 40,
    tokens_in: int = 120,
) -> MagicMock:
    """Build a fake LLM response with provider-style finish / stop reasons.

    ``finish_reason`` mirrors OpenAI + Bedrock Converse shape; ``stop_reason``
    mirrors Anthropic's shape. A real response only carries one of the two,
    but both keys coexisting is tolerated since the extractor falls through.
    """
    resp = MagicMock()
    resp.content = content
    meta: dict[str, Any] = {
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }
    if finish_reason is not None:
        meta["finish_reason"] = finish_reason
    if stop_reason is not None:
        meta["stop_reason"] = stop_reason
    resp.response_metadata = meta
    resp.usage_metadata = None
    return resp


# ---------------------------------------------------------------------------
# 1 & 2. Constant export + max_tokens wiring
# ---------------------------------------------------------------------------


def test_summarizer_max_output_tokens_default_is_1500():
    """The platform cap default is 1500 tokens (the Task 1 starting value)."""
    assert SUMMARIZER_MAX_OUTPUT_TOKENS == 1500


@pytest.mark.asyncio
async def test_max_tokens_forwarded_to_llm_ainvoke():
    """``summarize_slice`` must pass ``max_tokens=SUMMARIZER_MAX_OUTPUT_TOKENS``
    through to ``llm.ainvoke`` (either positionally via config / model-level
    binding, or as a direct kwarg). The observable contract: somewhere in the
    ainvoke call path the value 1500 must be visible."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_response()

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

    # The cap value (1500) must appear in either:
    #  - ``init_chat_model(..., max_tokens=1500)`` — model-level binding, or
    #  - ``llm.ainvoke(..., max_tokens=1500)`` — per-call binding.
    init_args = mock_init.call_args
    ainvoke_args = mock_llm.ainvoke.call_args

    found_in_init = (
        init_args is not None
        and init_args.kwargs.get("max_tokens") == SUMMARIZER_MAX_OUTPUT_TOKENS
    )
    found_in_ainvoke = False
    if ainvoke_args is not None:
        if ainvoke_args.kwargs.get("max_tokens") == SUMMARIZER_MAX_OUTPUT_TOKENS:
            found_in_ainvoke = True
        # Or embedded in a config dict
        for val in list(ainvoke_args.args) + list(ainvoke_args.kwargs.values()):
            if isinstance(val, dict) and val.get("max_tokens") == SUMMARIZER_MAX_OUTPUT_TOKENS:
                found_in_ainvoke = True
                break

    assert found_in_init or found_in_ainvoke, (
        f"Expected max_tokens={SUMMARIZER_MAX_OUTPUT_TOKENS} in init_chat_model "
        f"or llm.ainvoke call. init={init_args}, ainvoke={ainvoke_args}"
    )


# ---------------------------------------------------------------------------
# 3. WARN emission on truncation finish/stop reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warn_emitted_on_openai_length_finish_reason():
    """OpenAI / Bedrock Converse shape: ``finish_reason == 'length'`` fires WARN."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_response(
        content="x" * 4000,  # simulated 3k-token body truncated at cap
        finish_reason="length",
        tokens_out=1500,
    )

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        with patch("executor.compaction.summarizer._logger") as mock_logger:
            result = await summarize_slice(
                slice_messages=_two_message_slice(),
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
            )

    # Summary is still consumed despite truncation (replace-and-rehydrate).
    assert result.skipped is False
    assert result.summary_text is not None

    # Exactly one WARN with the expected event name + context fields.
    warn_calls = [c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "compaction.tier3_output_truncated"]
    assert len(warn_calls) == 1, (
        f"Expected exactly one compaction.tier3_output_truncated WARN, "
        f"got {len(warn_calls)} (all warning calls: {mock_logger.warning.call_args_list})"
    )
    warn_call = warn_calls[0]
    kwargs = warn_call.kwargs
    assert kwargs.get("tenant_id") == TENANT_ID
    assert kwargs.get("agent_id") == AGENT_ID
    assert kwargs.get("task_id") == TASK_ID
    assert kwargs.get("tokens_out") == 1500


@pytest.mark.asyncio
async def test_warn_emitted_on_anthropic_max_tokens_stop_reason():
    """Anthropic shape: ``stop_reason == 'max_tokens'`` fires WARN."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_response(
        content="y" * 4000,
        finish_reason=None,
        stop_reason="max_tokens",
        tokens_out=1500,
    )

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        with patch("executor.compaction.summarizer._logger") as mock_logger:
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
    warn_calls = [c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "compaction.tier3_output_truncated"]
    assert len(warn_calls) == 1
    kwargs = warn_calls[0].kwargs
    assert kwargs.get("tenant_id") == TENANT_ID
    assert kwargs.get("agent_id") == AGENT_ID
    assert kwargs.get("task_id") == TASK_ID
    assert kwargs.get("tokens_out") == 1500


# ---------------------------------------------------------------------------
# 4. No WARN on normal stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_warn_on_normal_stop_finish_reason():
    """``finish_reason='stop'`` (normal) must not emit the truncation WARN."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_response(finish_reason="stop", tokens_out=200)

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        with patch("executor.compaction.summarizer._logger") as mock_logger:
            await summarize_slice(
                slice_messages=_two_message_slice(),
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
            )

    warn_calls = [c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "compaction.tier3_output_truncated"]
    assert warn_calls == []


@pytest.mark.asyncio
async def test_no_warn_on_anthropic_end_turn_stop_reason():
    """Anthropic ``stop_reason='end_turn'`` (normal) must not emit the WARN."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_response(
        finish_reason=None, stop_reason="end_turn", tokens_out=200
    )

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        with patch("executor.compaction.summarizer._logger") as mock_logger:
            await summarize_slice(
                slice_messages=_two_message_slice(),
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
            )

    warn_calls = [c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "compaction.tier3_output_truncated"]
    assert warn_calls == []


@pytest.mark.asyncio
async def test_no_warn_when_finish_reason_missing():
    """Absent finish/stop reason (some test doubles, some providers) defaults
    to not-truncated — we prefer false-negative to false-positive WARNs."""
    ledger = _FakeCostLedger()
    fake_response = _make_fake_response(finish_reason=None, stop_reason=None)

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        with patch("executor.compaction.summarizer._logger") as mock_logger:
            await summarize_slice(
                slice_messages=_two_message_slice(),
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
            )

    warn_calls = [c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "compaction.tier3_output_truncated"]
    assert warn_calls == []


# ---------------------------------------------------------------------------
# 5 & 6. Prompt hardening properties
# ---------------------------------------------------------------------------


def test_prompt_states_token_budget():
    """Budget must be stated in tokens, not words — tokens are the load-bearing unit."""
    assert "token" in SUMMARIZER_PROMPT.lower()
    assert "500" in SUMMARIZER_PROMPT


def test_prompt_warns_about_truncation():
    """Prompt must tell the model over-budget output gets truncated."""
    assert "truncat" in SUMMARIZER_PROMPT.lower()


def test_prompt_instructs_preserve_recent_when_forced_to_choose():
    """Prompt must instruct preserving the most recent facts on truncation."""
    lowered = SUMMARIZER_PROMPT.lower()
    assert "recent" in lowered


def test_prompt_contains_concrete_example():
    """A concrete example makes the cap binding rather than advisory."""
    lowered = SUMMARIZER_PROMPT.lower()
    assert "example" in lowered or "e.g." in lowered


def test_prompt_retains_preservation_priorities():
    """No regression on the preservation-priority list (files / URLs /
    decisions / errors / identifiers)."""
    lowered = SUMMARIZER_PROMPT.lower()
    assert "file" in lowered
    assert "url" in lowered
    assert "decision" in lowered
    assert "error" in lowered
    # Identifiers in the old prompt were phrased as "parameters or identifiers
    # (IDs, keys, names)"; accept any of those anchors to avoid over-specifying
    # the exact wording.
    assert any(tok in lowered for tok in ("identifier", "id", "key", "name"))
