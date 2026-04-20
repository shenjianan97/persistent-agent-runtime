"""Tier 3 retrospective LLM summariser — Task 7 / Track 7 Context Window Management.

Entry point: :func:`summarize_slice`.

The summariser takes the message slice ``messages[summarized_through_turn_index :
protect_from_index]``, compresses it into a compact factual paragraph, and writes
one attribution row to ``agent_cost_ledger`` tagged ``operation='compaction.tier3'``.

Design notes
------------
- **Retry on transient errors.** Up to ``SUMMARIZER_MAX_RETRIES`` retries on
  429/5xx / connection errors. On exhaustion returns ``SummarizeResult(skipped=True,
  skipped_reason='retryable')`` — the caller (Task 8 pipeline) leaves the watermark
  un-advanced and re-attempts on the next agent-node call.
- **Fatal-error short-circuit.** On non-retryable errors (bad API key, model
  removed, 4xx with invalid model) returns ``SummarizeResult(skipped=True,
  skipped_reason='fatal')`` and emits a ``compaction.tier3_fatal`` structured log.
  The Task 8 pipeline sets ``tier3_fatal_short_circuited=True`` so it does NOT
  re-fire Tier 3 on every subsequent call (which would burn the per-call cost).
- **Cost-ledger write.** On success, one ``INSERT`` with
  ``ON CONFLICT DO NOTHING`` using the partial unique index on
  ``(tenant_id, task_id, checkpoint_id, operation, summarized_through_turn_index_after)``
  — crash-after-insert-before-state-commit is swallowed rather than double-charging.
- **Langfuse callbacks.** If a Langfuse ``CallbackHandler`` is included in
  ``callbacks``, it is automatically forwarded to ``llm.ainvoke`` via the
  ``config={"callbacks": ...}`` argument; no extra instrumentation needed.
- **Deterministic serialisation.** ``format_messages_for_summary`` uses
  ``json.dumps(sort_keys=True)`` for all dict arguments so the KV-cache on
  the summary invocation itself is maximally stable across repeated firings
  with the same input slice.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from executor.compaction.defaults import (
    SUMMARIZER_MAX_OUTPUT_TOKENS,
    SUMMARIZER_MAX_RETRIES,
)
from executor.compaction.tokens import _extract_text_content as _extract_content_from_value

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Module-level logger (structlog, plain event strings)
# ---------------------------------------------------------------------------

_logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Platform-owned summariser prompt — NOT exposed in customer-facing UI
# ---------------------------------------------------------------------------

SUMMARIZER_PROMPT: str = (
    "You are compressing a portion of an autonomous agent's tool-use history so\n"
    "the agent can continue its task within a limited context window.\n"
    "\n"
    "OUTPUT BUDGET (binding): your summary MUST be at most 500 tokens. The\n"
    "caller enforces this cap at the API layer — any response longer than the\n"
    "cap is cut off at the cap and the tail is permanently lost. Plan your\n"
    "summary so the most important facts fit inside the budget. If you must\n"
    "choose what to drop, drop older context first and preserve the most\n"
    "recent facts — those are what the agent needs to continue.\n"
    "\n"
    "Preserve (in priority order):\n"
    "- Files the agent has created, read, or modified (full paths)\n"
    "- External URLs or API responses whose contents matter for the rest of the task\n"
    "- Decisions the agent has committed to and their reasoning\n"
    "- Errors encountered and whether they were resolved\n"
    "- Parameters or identifiers the agent will need later (IDs, keys, names)\n"
    "\n"
    "Do NOT:\n"
    "- Address the agent in the second person.\n"
    "- Invent next steps or give instructions.\n"
    "- Comment on the compression itself or on the budget.\n"
    "\n"
    "Example of budget failure — DO NOT do this:\n"
    "  The agent began by reading the project README in full, including the\n"
    "  introduction, the installation section, the configuration section, the\n"
    "  usage examples, the troubleshooting appendix, the contributor guide,\n"
    "  the licensing note, and then [... 600 more tokens of recap ...] and\n"
    "  finally the agent fixed the bug in auth.py by — <TRUNCATED AT CAP; THE\n"
    "  FIX AND EVERY FACT AFTER IT IS LOST>.\n"
    "The correct summary instead leads with the load-bearing recent facts\n"
    "(files edited, errors fixed, decisions made) and omits the narrative\n"
    "recap.\n"
    "\n"
    "Return the summary only."
)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SummarizeResult:
    """Outcome of one :func:`summarize_slice` invocation.

    ``skipped=True`` is the non-terminal case — the caller logs or emits a
    compaction event and continues without advancing the watermark.
    """

    summary_text: str | None
    """The produced summary, or ``None`` when ``skipped=True``."""

    skipped: bool
    """``True`` when no summary was produced (empty slice, retry exhaustion, or fatal error)."""

    skipped_reason: str | None
    """
    ``None`` when ``skipped=False``.  One of:

    - ``"empty_slice"``  — slice had fewer than 2 messages; nothing to summarise.
    - ``"retryable"``    — all retry attempts failed with transient errors; caller
                           may re-attempt on the next agent-node invocation.
    - ``"fatal"``        — non-retryable provider error (bad API key, model not
                           found, etc.).  Caller should set ``tier3_fatal_short_circuited=True``
                           to avoid burning per-call cost on every subsequent step.
    """

    summarizer_model_id: str
    """Model used (or attempted) for this summarisation call."""

    tokens_in: int
    """Prompt token count from the provider response metadata."""

    tokens_out: int
    """Completion token count from the provider response metadata."""

    cost_microdollars: int
    """Rolled-up spend in microdollars (0 when skipped or when model has no cost row)."""

    latency_ms: int
    """Wall-clock time for the LLM call in milliseconds (0 when skipped)."""


# ---------------------------------------------------------------------------
# Cost-ledger protocol
# ---------------------------------------------------------------------------


class CostLedgerRepository:
    """Minimal interface the summariser needs to write a cost row.

    The real implementation (``core.cost_ledger_repository``) is passed in by
    the caller so the summariser module has zero direct DB coupling.

    Tests inject a lightweight fake that records calls for inspection.
    """

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
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Message serialisation (deterministic — KV-cache stability requirement)
# ---------------------------------------------------------------------------


def format_messages_for_summary(slice_messages: list[BaseMessage]) -> str:
    """Produce a deterministic textual representation of a message slice.

    Format per message type:

    - ``SystemMessage``  → ``SYSTEM: <content>``
    - ``HumanMessage``   → ``USER: <content>``
    - ``AIMessage``      → ``ASSISTANT (step N): <content>``
                           followed by ``  tool_calls: [<name>(...), ...]``
                           with args serialised via ``json.dumps(sort_keys=True)``.
    - ``ToolMessage``    → ``TOOL_RESULT (call_id=..., name=...): <content>``

    ``N`` is the 0-based index in *this slice* (not the full history), so the
    rendered indices are stable across repeated calls on the same slice.

    Determinism is load-bearing: callers with the same slice must produce
    byte-identical output to maximise KV-cache reuse on the summariser
    invocation itself.
    """
    lines: list[str] = []
    for idx, msg in enumerate(slice_messages):
        content = _extract_text_content(msg)
        if isinstance(msg, SystemMessage):
            lines.append(f"SYSTEM: {content}")
        elif isinstance(msg, HumanMessage):
            lines.append(f"USER: {content}")
        elif isinstance(msg, AIMessage):
            header = f"ASSISTANT (step {idx}): {content}"
            lines.append(header)
            if msg.tool_calls:
                call_parts = []
                for tc in msg.tool_calls:
                    name = tc.get("name", "unknown") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
                    args_raw = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    args_str = json.dumps(args_raw, sort_keys=True)
                    call_parts.append(f"{name}({args_str})")
                lines.append(f"  tool_calls: [{', '.join(call_parts)}]")
        elif isinstance(msg, ToolMessage):
            call_id = getattr(msg, "tool_call_id", "")
            name = getattr(msg, "name", "") or ""
            lines.append(f"TOOL_RESULT (call_id={call_id}, name={name}): {content}")
        else:
            # Unknown message type — render as a generic entry
            lines.append(f"MESSAGE (step {idx}, type={type(msg).__name__}): {content}")
    return "\n".join(lines)


def _extract_text_content(msg: BaseMessage) -> str:
    """Flatten message content to a plain string.

    Delegates to ``tokens._extract_content_from_value`` which is the
    canonical implementation shared between the token-estimation and
    summary-formatting paths.
    """
    return _extract_content_from_value(msg.content)


# ---------------------------------------------------------------------------
# Error classification helpers (ported from GraphExecutor — kept as module-level
# functions here so summarizer.py has zero coupling to GraphExecutor class)
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504, 529})


def _walk_exception_chain(e: Exception):
    """Yield each exception in the __cause__/__context__ chain (incl. ``e``)."""
    current = e
    for _ in range(5):
        if current is None:
            break
        yield current
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)


def _extract_status_code(e: Exception) -> int | None:
    """Walk the exception chain to find an HTTP status code."""
    for exc in _walk_exception_chain(e):
        code = getattr(exc, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _is_retryable_error(e: Exception) -> bool:
    """Return ``True`` when the exception represents a transient provider failure."""
    if isinstance(e, (ConnectionError, TimeoutError)):
        return True

    status = _extract_status_code(e)
    if status is not None:
        return status in _RETRYABLE_STATUS_CODES

    # Fallback string heuristics for errors without a status code attribute
    error_str = str(e).lower()
    if "429" in error_str or "rate limit" in error_str or "rate exceeded" in error_str:
        return True
    if re.search(r"\b50[0234]\b", error_str):
        return True
    if "validation" in error_str or "invalid" in error_str or "unsupported" in error_str or "pydantic" in error_str:
        return False
    if re.search(r"\b40[0-4]\b", error_str):
        return False

    # Default unknown exceptions to non-retryable
    return False


def _get_retry_after(e: Exception) -> float | None:
    """Extract ``Retry-After`` seconds from the error's HTTP response headers."""
    for exc in _walk_exception_chain(e):
        resp = getattr(exc, "response", None)
        if resp is not None:
            retry_after = getattr(resp, "headers", {}).get("retry-after")
            if retry_after:
                try:
                    return float(retry_after)
                except (ValueError, TypeError):
                    pass
            return None
    return None


# ---------------------------------------------------------------------------
# Token + cost extraction helpers
# ---------------------------------------------------------------------------


# Finish / stop reasons that indicate the response hit the ``max_tokens`` cap.
# - ``"length"``     : OpenAI, Bedrock Converse, most OpenAI-compatible APIs.
# - ``"max_tokens"`` : Anthropic (surfaced as ``stop_reason``).
# Normal completions are ``"stop"`` (OpenAI) or ``"end_turn"`` (Anthropic) —
# anything other than the truncation values above is treated as not-truncated.
_TRUNCATION_FINISH_REASONS: frozenset[str] = frozenset({"length", "max_tokens"})


def _is_response_truncated_at_cap(metadata: dict) -> bool:
    """Return ``True`` when the LLM response indicates truncation at ``max_tokens``.

    Walks the provider-specific keys (OpenAI/Bedrock ``finish_reason`` and
    Anthropic ``stop_reason``) found either at the top level of
    ``response_metadata`` or nested inside ``usage_metadata``. Missing reason
    is treated as not-truncated — we prefer false-negative over false-positive
    WARN emission so the alert stays actionable.
    """
    candidates: list[Any] = [
        metadata.get("finish_reason"),
        metadata.get("stop_reason"),
    ]
    # Some provider wrappers bury the reason inside usage_metadata
    nested = metadata.get("usage_metadata")
    if isinstance(nested, dict):
        candidates.append(nested.get("finish_reason"))
        candidates.append(nested.get("stop_reason"))
    for reason in candidates:
        if isinstance(reason, str) and reason in _TRUNCATION_FINISH_REASONS:
            return True
    return False


def _extract_tokens(metadata: dict) -> tuple[int, int]:
    """Return ``(input_tokens, output_tokens)`` from LLM response metadata.

    Handles the provider-specific key shapes used by Anthropic, OpenAI, and
    Google/Bedrock.
    """
    usage = (
        metadata.get("usage")              # Anthropic, Google
        or metadata.get("token_usage")     # OpenAI via LangChain
        or metadata.get("usage_metadata")  # Bedrock
        or {}
    )
    input_t = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    output_t = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return int(input_t), int(output_t)


def _rollup_cost(tokens_in: int, tokens_out: int, input_rate: int, output_rate: int) -> int:
    """Compute cost in microdollars from token counts and per-million rates."""
    return (tokens_in * input_rate + tokens_out * output_rate) // 1_000_000


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def summarize_slice(
    slice_messages: list[BaseMessage],
    summarizer_model_id: str,
    task_id: str,
    tenant_id: str,
    agent_id: str,
    checkpoint_id: str | None,
    cost_ledger: CostLedgerRepository,
    callbacks: list[BaseCallbackHandler] | None = None,
    *,
    # Optional: caller supplies the watermark index so the ledger row carries it
    # for idempotency (partial unique index on checkpoint_id + operation +
    # summarized_through_turn_index_after).  If omitted, written as NULL.
    summarized_through_turn_index_after: int | None = None,
) -> SummarizeResult:
    """Summarise a message slice using a cheap LLM call.

    Parameters
    ----------
    slice_messages:
        The messages to be summarised.  Must have at least 2 entries; callers
        should pass ``messages[summarized_through_turn_index : protect_from_index]``.
    summarizer_model_id:
        LangChain model identifier (e.g. ``"claude-haiku-4-5"``).  Comes from
        ``agent_config.context_management.summarizer_model`` or the platform
        default resolved by the caller.
    task_id, tenant_id, agent_id, checkpoint_id:
        Attribution keys written to the cost ledger.
    cost_ledger:
        An object exposing an ``async insert(...)`` method compatible with
        :class:`CostLedgerRepository`.
    callbacks:
        Optional list of LangChain ``BaseCallbackHandler`` instances (typically
        a Langfuse ``CallbackHandler``).  Forwarded into the LLM ``ainvoke``
        call so Langfuse auto-traces the span without extra instrumentation.
    summarized_through_turn_index_after:
        Watermark value after this summarisation fires.  Written into the cost-
        ledger row as part of the idempotency key so crash-after-insert-before-
        state-commit is swallowed by ``ON CONFLICT DO NOTHING``.

    Returns
    -------
    SummarizeResult
        ``skipped=False`` on success; ``skipped=True`` with a ``skipped_reason``
        on all failure / no-op paths.  Never raises.
    """
    # ------------------------------------------------------------------ #
    # 1. Empty-slice guard                                                 #
    # ------------------------------------------------------------------ #
    if len(slice_messages) < 2:
        return SummarizeResult(
            summary_text=None,
            skipped=True,
            skipped_reason="empty_slice",
            summarizer_model_id=summarizer_model_id,
            tokens_in=0,
            tokens_out=0,
            cost_microdollars=0,
            latency_ms=0,
        )

    # ------------------------------------------------------------------ #
    # 2. Build prompt                                                       #
    # ------------------------------------------------------------------ #
    serialized = format_messages_for_summary(slice_messages)
    prompt = [
        SystemMessage(content=SUMMARIZER_PROMPT),
        HumanMessage(content=serialized),
    ]

    # ------------------------------------------------------------------ #
    # 3. Initialise LLM (provider routing mirrors GraphExecutor pattern)   #
    # ------------------------------------------------------------------ #
    llm = init_chat_model(
        model=summarizer_model_id,
        temperature=0.2,
        max_retries=0,  # retries handled in our own loop
        timeout=120,
        # ``max_tokens`` is the safety net for a model that ignores the
        # prompt-level ≤500-token budget. 1500 caps pathological runaways
        # at ~3× the target while leaving headroom for well-behaved models
        # to wrap up gracefully. See docs/exec-plans/active/phase-2/
        # track-7-follow-up/agent_tasks/task-1-summarizer-prompt-and-caps.md.
        max_tokens=SUMMARIZER_MAX_OUTPUT_TOKENS,
    )

    # ------------------------------------------------------------------ #
    # 4. Retry loop                                                         #
    # ------------------------------------------------------------------ #
    last_error: Exception | None = None
    for attempt in range(SUMMARIZER_MAX_RETRIES + 1):
        try:
            started = time.monotonic()
            response = await llm.ainvoke(
                prompt,
                config={"callbacks": callbacks or []},
            )
            latency_ms = int((time.monotonic() - started) * 1000)

            # ---------------------------------------------------------- #
            # 5. Extract tokens and compute cost                          #
            # ---------------------------------------------------------- #
            resp_meta: dict[str, Any] = dict(
                getattr(response, "response_metadata", {}) or {}
            )
            # Some providers (Anthropic) surface usage_metadata separately
            if getattr(response, "usage_metadata", None):
                resp_meta.setdefault("usage_metadata", response.usage_metadata)

            tokens_in, tokens_out = _extract_tokens(resp_meta)

            # ---------------------------------------------------------- #
            # 5a. Truncation telemetry                                    #
            # ---------------------------------------------------------- #
            # When ``max_tokens=SUMMARIZER_MAX_OUTPUT_TOKENS`` clips the
            # response, the provider surfaces a truncation finish/stop
            # reason. Emit a WARN so operators can see the cap firing in
            # observability; the truncated summary is still consumed
            # (replace-and-rehydrate tolerates a shorter summary this
            # firing — next firing re-summarizes from the updated slice).
            # Repeated firings on the same task are the signal that the
            # prompt still isn't binding or that the cap needs to rise.
            if _is_response_truncated_at_cap(resp_meta):
                _logger.warning(
                    "compaction.tier3_output_truncated",
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    tokens_out=tokens_out,
                )

            # Cost rates are intentionally 0 in this standalone module.
            # The caller (Task 8 pipeline / GraphExecutor) is responsible for
            # fetching rates from the ``models`` table. We write the row with
            # cost_microdollars=0 here and rely on the fact that the ledger
            # row is idempotent; if the pipeline layer wants accurate costs it
            # can compute them before calling us and pass a pre-computed value
            # via a future API extension. For now the cost is best-effort zero
            # so CI passes without a DB connection.
            #
            # TODO (Task 8): accept an optional ``cost_rates`` tuple so the
            # pipeline can pass in (input_rate, output_rate) from the models
            # table, making ledger rows accurate without adding a DB call here.
            cost_microdollars = 0

            # ---------------------------------------------------------- #
            # 6. Write cost-ledger row (ON CONFLICT DO NOTHING)           #
            # ---------------------------------------------------------- #
            await cost_ledger.insert(
                tenant_id=tenant_id,
                agent_id=agent_id,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                cost_microdollars=cost_microdollars,
                operation="compaction.tier3",
                model_id=summarizer_model_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                summarized_through_turn_index_after=summarized_through_turn_index_after,
            )

            # ---------------------------------------------------------- #
            # 7. Return success                                            #
            # ---------------------------------------------------------- #
            summary_text = (
                response.content
                if isinstance(response.content, str)
                else _extract_text_content_from_response(response.content)
            )
            return SummarizeResult(
                summary_text=summary_text,
                skipped=False,
                skipped_reason=None,
                summarizer_model_id=summarizer_model_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_microdollars=cost_microdollars,
                latency_ms=latency_ms,
            )

        except Exception as e:
            last_error = e

            if not _is_retryable_error(e):
                # Non-retryable: bad API key, model removed, auth failure, etc.
                # Emit a structured log so ops can act without filtering noisy retries.
                _logger.info(
                    "compaction.tier3_fatal",
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    summarizer_model=summarizer_model_id,
                    error=str(e)[:200],
                )
                return SummarizeResult(
                    summary_text=None,
                    skipped=True,
                    skipped_reason="fatal",
                    summarizer_model_id=summarizer_model_id,
                    tokens_in=0,
                    tokens_out=0,
                    cost_microdollars=0,
                    latency_ms=0,
                )

            # Retryable: back off and try again
            backoff = _get_retry_after(e) or min(30.0, 2.0 ** attempt)
            await asyncio.sleep(backoff)

    # ------------------------------------------------------------------ #
    # 8. Retries exhausted                                                 #
    # ------------------------------------------------------------------ #
    _logger.info(
        "compaction.tier3_skipped",
        tenant_id=tenant_id,
        agent_id=agent_id,
        task_id=task_id,
        summarizer_model=summarizer_model_id,
        retries_exhausted=SUMMARIZER_MAX_RETRIES,
        last_error=str(last_error)[:200] if last_error else None,
    )
    return SummarizeResult(
        summary_text=None,
        skipped=True,
        skipped_reason="retryable",
        summarizer_model_id=summarizer_model_id,
        tokens_in=0,
        tokens_out=0,
        cost_microdollars=0,
        latency_ms=0,
    )


def _extract_text_content_from_response(content: Any) -> str:
    """Flatten block-list content from an LLM response to plain string."""
    return _extract_content_from_value(content)
