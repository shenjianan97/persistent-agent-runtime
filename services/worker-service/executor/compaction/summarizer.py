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
    SUMMARIZER_INPUT_HEADROOM_TOKENS,
    SUMMARIZER_MAX_OUTPUT_TOKENS,
    SUMMARIZER_MAX_RETRIES,
)
from executor.compaction.tokens import _extract_text_content as _extract_content_from_value

_SUMMARIZER_MAX_OUTPUT_TOKENS = SUMMARIZER_MAX_OUTPUT_TOKENS

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
    "You are compressing a portion of an autonomous agent's tool-use history so the\n"
    "agent can continue the task within its context window. Produce a compact\n"
    "factual summary bounded by the caller-enforced output cap\n"
    "(SUMMARIZER_MAX_OUTPUT_TOKENS, currently 1500 tokens) — anything past the\n"
    "cap is truncated and the tail is permanently lost.\n"
    "\n"
    "THE SINGLE MOST IMPORTANT RULE: collapse homogeneous tool_call batches.\n"
    "When an AIMessage issues many tool_calls of the same name with clearly\n"
    "patterned arguments (e.g. 20 read_url calls on different Wikipedia article\n"
    "paths, 30 read_file calls under one directory, 10 search_web queries on a\n"
    "common topic), describe them ONCE as a count + pattern + the purpose they\n"
    "served. Do NOT list each individual call or URL/path. Example:\n"
    "  BAD:  read_url(en.wikipedia.org/Ancient_Egypt), read_url(en.wikipedia.org/Ancient_Rome),\n"
    "        read_url(en.wikipedia.org/Ancient_Greece), ... [47 more]\n"
    "  GOOD: Issued 50 parallel read_url calls against en.wikipedia.org covering\n"
    "        ancient civilizations (Egypt, Rome, Greece, ...) and world religions;\n"
    "        synthesized into a periodization of political ideologies.\n"
    "A batch is homogeneous when the tool name is the same AND arguments share a\n"
    "clear template (same domain, same key with varying value, same directory with\n"
    "varying filenames). One or two representative examples plus the count and\n"
    "shape of the remainder is enough.\n"
    "\n"
    "PRESERVE individual tool calls when they carry unique, non-redundant\n"
    "information that future turns will need. The rule of thumb: if removing a\n"
    "specific call would lose information the agent cannot reconstruct, keep it.\n"
    "Typical keepers:\n"
    "- Single write_file / edit_file / create_text_artifact — record path + intent.\n"
    "- memory_note / save_memory with distinct content — record the note verbatim\n"
    "  or near-verbatim; these are load-bearing for later turns.\n"
    "- A one-off search or fetch whose result changed the plan — record both the\n"
    "  query and the key finding.\n"
    "- Any tool call whose args are heterogeneous (not reducible to a pattern).\n"
    "\n"
    "ALSO PRESERVE (these drive future turns and must not be collapsed away):\n"
    "- Files the agent has created, read, or modified — full paths.\n"
    "- External URLs or API responses whose contents matter for the rest of the task.\n"
    "- Decisions the agent has committed to, and their reasoning.\n"
    "- The user's evolving intent across turns: the original ask, any follow-up\n"
    "  questions, human-in-the-loop resumes, clarifications, and explicit\n"
    "  preferences (e.g. 'in markdown', 'use Python 3.12', 'avoid X').\n"
    "- Errors encountered and whether they were resolved.\n"
    "- Parameters or identifiers the agent will need later (IDs, keys, names).\n"
    "- Facts the agent has learned from tools that it cannot re-derive cheaply.\n"
    "\n"
    "STRUCTURE your output as short labelled sections, not free prose. Suggested\n"
    "headings (omit any that are empty):\n"
    "- User intent & preferences:\n"
    "- Decisions & rationale:\n"
    "- Files touched:\n"
    "- Key facts learned:\n"
    "- Tool activity (collapsed): <one line per homogeneous batch>\n"
    "- Unresolved errors / open questions:\n"
    "Structured bullets are cheaper than narrative and survive the output cap.\n"
    "\n"
    "MERGING WITH A PRIOR SUMMARY. If the serialized slice begins with an entry\n"
    "labelled 'PRIOR_SUMMARY:' or with a SystemMessage that is itself a summary,\n"
    "treat it as established context: merge its facts into the new output rather\n"
    "than copying it verbatim, and drop anything it covered that the new middle\n"
    "has superseded. The output is one coherent summary, not a concatenation.\n"
    "\n"
    "Do NOT:\n"
    "- Address the agent in the second person.\n"
    "- Invent next steps or give instructions.\n"
    "- Comment on the compression itself (no 'I have summarized...').\n"
    "- Re-list the members of a homogeneous batch. Collapse is the mechanism that\n"
    "  keeps the summary under its token cap — listing every call defeats it.\n"
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
    # Task 2 additions — recursive chunking for oversized middles.
    prior_summary: str = "",
    summarizer_context_window: int | None = None,
) -> SummarizeResult:
    """Summarise a message slice using a cheap LLM call.

    When ``summarizer_context_window`` is supplied and the full summariser
    payload (SUMMARIZER_PROMPT + prior_summary + serialised middle + output
    reservation + SUMMARIZER_INPUT_HEADROOM_TOKENS) fits inside it, exactly
    one LLM call is made — byte-for-byte identical to the pre-Task-2 path.
    Otherwise the middle is split in halves (safe-boundary-aligned where
    possible, unsafe halving with a WARN when no interior safe boundary
    exists), each half is recursively summarised, and a final concatenation
    call on the synthetic middle of chunk summaries carries the original
    ``prior_summary``.

    Callers that don't pass ``summarizer_context_window`` (notably the
    legacy Track 7 pipeline) get the single-call path unconditionally —
    same behaviour as before Task 2.

    Parameters
    ----------
    slice_messages:
        The messages to be summarised. In the Task-3 `pre_model_hook` caller
        this is the "middle" between ``summarized_through`` and
        ``keep_window_start``. Must have at least 2 entries.
    summarizer_model_id:
        LangChain model identifier (e.g. ``"claude-haiku-4-5"``).
    task_id, tenant_id, agent_id, checkpoint_id:
        Attribution keys written to the cost ledger.
    cost_ledger:
        An object exposing an ``async insert(...)`` method compatible with
        :class:`CostLedgerRepository`.
    callbacks:
        Optional LangChain callbacks forwarded to ``llm.ainvoke``.
    summarized_through_turn_index_after:
        Watermark value written into the cost-ledger row as part of the
        idempotency key.
    prior_summary:
        Existing summary string (if any) to be concatenated with the
        serialised middle in the HumanMessage. Carried at the TOP-LEVEL
        call only — recursive per-chunk calls pass ``prior_summary=""``
        and re-introduce the original summary on the final concatenation
        call. Default ``""`` preserves pre-Task-2 behaviour.
    summarizer_context_window:
        Effective context window of the summariser model. When ``None``,
        chunking is disabled and the call always takes the fast path.

    Returns
    -------
    SummarizeResult
        ``skipped=False`` on success; ``skipped=True`` with a ``skipped_reason``
        on all failure / no-op paths. Never raises. Token / cost / latency
        fields accumulate across all sub-calls when chunking fires.
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
    # 2. Payload-fit gate — fast path vs recurse.                          #
    # ------------------------------------------------------------------ #
    if summarizer_context_window is not None and not _payload_fits(
        slice_messages=slice_messages,
        prior_summary=prior_summary,
        summarizer_context_window=summarizer_context_window,
    ):
        return await _chunk_summarize(
            middle_messages=slice_messages,
            prior_summary=prior_summary,
            operation="compaction.tier3",
            summarizer_model_id=summarizer_model_id,
            task_id=task_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            checkpoint_id=checkpoint_id,
            cost_ledger=cost_ledger,
            callbacks=callbacks,
            summarized_through_turn_index_after=summarized_through_turn_index_after,
            summarizer_context_window=summarizer_context_window,
        )

    # ------------------------------------------------------------------ #
    # 3. Fast path — one LLM call.                                         #
    # ------------------------------------------------------------------ #
    return await _summarize_single_call(
        slice_messages=slice_messages,
        prior_summary=prior_summary,
        operation="compaction.tier3",
        summarizer_model_id=summarizer_model_id,
        task_id=task_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        checkpoint_id=checkpoint_id,
        cost_ledger=cost_ledger,
        callbacks=callbacks,
        summarized_through_turn_index_after=summarized_through_turn_index_after,
    )


# ---------------------------------------------------------------------------
# Single-call primitive — extracted from the previous summarize_slice body so
# both the fast path and the recursive chunker can share the retry loop and
# ledger-row schema. Behaviour parity check: when called with
# ``prior_summary=""`` and ``operation="compaction.tier3"`` the HumanMessage
# content and ledger row are byte-identical to the pre-Task-2 implementation.
# ---------------------------------------------------------------------------


async def _summarize_single_call(
    *,
    slice_messages: list[BaseMessage],
    prior_summary: str,
    operation: str,
    summarizer_model_id: str,
    task_id: str,
    tenant_id: str,
    agent_id: str,
    checkpoint_id: str | None,
    cost_ledger: CostLedgerRepository,
    callbacks: list[BaseCallbackHandler] | None,
    summarized_through_turn_index_after: int | None,
) -> SummarizeResult:
    """Execute one summariser LLM call with the module's retry policy.

    Builds the HumanMessage as ``prior_summary + serialised middle``
    (``prior_summary`` is inserted only when non-empty and separated by a
    blank line for readability). Writes one cost-ledger row tagged with
    ``operation`` on success; writes nothing on failure.
    """
    # ------------------------------------------------------------------ #
    # Build prompt                                                         #
    # ------------------------------------------------------------------ #
    serialized = format_messages_for_summary(slice_messages)
    _framing = (
        "Summarize the following agent tool-use history into one coherent\n"
        "summary, following the rules in the system prompt. Remember: collapse\n"
        "homogeneous tool_call batches (same tool name + patterned args) into a\n"
        "count + pattern line; preserve one-off calls, decisions, user intent,\n"
        "and facts the agent cannot cheaply re-derive.\n\n"
    )
    if prior_summary:
        human_content = (
            _framing
            + "PRIOR SUMMARY (context from earlier compaction — not part of the "
            "slice below):\n"
            f"{prior_summary}\n\n"
            "MESSAGES TO COMPRESS:\n"
            f"{serialized}"
        )
    else:
        human_content = _framing + "MESSAGES TO COMPRESS:\n" + serialized

    prompt = [
        SystemMessage(content=SUMMARIZER_PROMPT),
        HumanMessage(content=human_content),
    ]

    # ------------------------------------------------------------------ #
    # Initialise LLM                                                        #
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
    # Retry loop                                                            #
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

            resp_meta: dict[str, Any] = dict(
                getattr(response, "response_metadata", {}) or {}
            )
            if getattr(response, "usage_metadata", None):
                resp_meta.setdefault("usage_metadata", response.usage_metadata)

            tokens_in, tokens_out = _extract_tokens(resp_meta)

            # Truncation telemetry: when ``max_tokens=SUMMARIZER_MAX_OUTPUT_TOKENS``
            # clips the response, the provider surfaces a truncation finish/stop
            # reason. Emit a WARN so operators can see the cap firing in
            # observability; the truncated summary is still consumed.
            if _is_response_truncated_at_cap(resp_meta):
                _logger.warning(
                    "compaction.tier3_output_truncated",
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    tokens_out=tokens_out,
                )

            # See note in summarize_slice docstring — cost is best-effort 0
            # in this standalone module; the pipeline layer owns rate lookup.
            cost_microdollars = 0

            await cost_ledger.insert(
                tenant_id=tenant_id,
                agent_id=agent_id,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                cost_microdollars=cost_microdollars,
                operation=operation,
                model_id=summarizer_model_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                summarized_through_turn_index_after=summarized_through_turn_index_after,
            )

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
                _logger.info(
                    "compaction.tier3_fatal",
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    summarizer_model=summarizer_model_id,
                    operation=operation,
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

            backoff = _get_retry_after(e) or min(30.0, 2.0 ** attempt)
            await asyncio.sleep(backoff)

    _logger.info(
        "compaction.tier3_skipped",
        tenant_id=tenant_id,
        agent_id=agent_id,
        task_id=task_id,
        summarizer_model=summarizer_model_id,
        operation=operation,
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


# ---------------------------------------------------------------------------
# Recursive chunk summariser — Task 2
# ---------------------------------------------------------------------------


def _heuristic_token_count(text: str) -> int:
    """Crude char/3 token estimate used for the payload-fit gate.

    We intentionally avoid provider-specific tokenisers here: the gate runs
    on every summariser entry and needs to be deterministic and cheap. The
    heuristic is the same fallback path used inside
    ``executor.compaction.tokens.estimate_tokens`` — it overestimates
    slightly for English text, which is the safe direction for a gate.
    """
    if not text:
        return 0
    return max(1, len(text.encode("utf-8")) // 3)


def _estimate_payload_tokens(
    slice_messages: list[BaseMessage],
    prior_summary: str,
) -> int:
    """Estimate the full summariser payload in tokens.

    Mirrors the exact text we send to ``llm.ainvoke``:
    ``SUMMARIZER_PROMPT`` (SystemMessage) + ``prior_summary`` (if any, with
    the separator preamble built in ``_summarize_single_call``) + serialised
    middle (via :func:`format_messages_for_summary`).

    Does NOT include the ``max_tokens`` output reservation or the
    ``SUMMARIZER_INPUT_HEADROOM_TOKENS`` safety budget — those are added in
    :func:`_payload_fits` so this function can be reused elsewhere.
    """
    serialized_middle = format_messages_for_summary(slice_messages)
    if prior_summary:
        # Match the separator that _summarize_single_call actually emits so
        # the gate estimate tracks the real HumanMessage byte count.
        human_content = (
            "PRIOR SUMMARY (context from earlier compaction — not part of the "
            "slice below):\n"
            f"{prior_summary}\n\n"
            "MESSAGES TO COMPRESS:\n"
            f"{serialized_middle}"
        )
    else:
        human_content = serialized_middle
    return (
        _heuristic_token_count(SUMMARIZER_PROMPT)
        + _heuristic_token_count(human_content)
    )


def _payload_fits(
    *,
    slice_messages: list[BaseMessage],
    prior_summary: str,
    summarizer_context_window: int,
) -> bool:
    """Return ``True`` when the full summariser payload fits in the window.

    Budget accounting:

        prompt + prior_summary + serialised middle
            + SUMMARIZER_MAX_OUTPUT_TOKENS (response reservation)
            + SUMMARIZER_INPUT_HEADROOM_TOKENS (safety margin)
        <= summarizer_context_window
    """
    payload_tokens = _estimate_payload_tokens(slice_messages, prior_summary)
    budget_used = (
        payload_tokens
        + _SUMMARIZER_MAX_OUTPUT_TOKENS
        + SUMMARIZER_INPUT_HEADROOM_TOKENS
    )
    return budget_used <= summarizer_context_window


def _find_safe_split(middle: list[BaseMessage]) -> tuple[int, bool]:
    """Pick a safe chunk-split index for recursion.

    Starts from the natural midpoint ``len(middle) // 2`` and walks back to
    the nearest index ``j`` such that ``middle[j]`` is NOT a ``ToolMessage``.
    If the nearest non-ToolMessage is an ``AIMessage`` with ``tool_calls``,
    we walk back one more step so the split does not orphan the
    AIMessage/ToolMessage pair on the right-hand half.

    Returns ``(split_index, is_unsafe_fallback)``:
    - ``split_index`` — a non-negative integer. When ``is_unsafe_fallback``
      is ``False``, the split lands on a safe boundary (anything other than
      an AIMessage-with-tool_calls immediately followed by its ToolMessage
      replies).
    - ``is_unsafe_fallback`` — ``True`` when no interior safe boundary
      exists and we fell back to unsafe halving at ``len(middle) // 2``.

    Progress guarantee: the returned ``split_index`` always satisfies
    ``0 < split_index < len(middle)``.
    """
    n = len(middle)
    assert n >= 2, "caller must guard — _find_safe_split needs >= 2 messages"

    midpoint = n // 2
    # midpoint in [1, n-1] for n>=2 (since n//2 >= 1 for n >= 2).
    # Walk back from midpoint to find an index whose message is NOT a
    # ToolMessage AND whose predecessor is NOT an AIMessage-with-tool_calls
    # that would be orphaned by the split.
    for j in range(midpoint, 0, -1):
        candidate = middle[j]
        if isinstance(candidate, ToolMessage):
            continue
        # Candidate is a HumanMessage, AIMessage, or SystemMessage. If
        # candidate is an AIMessage-with-tool_calls, splitting at j keeps
        # the pair together (the AI-with-tool_calls goes to the right half
        # along with its ToolMessage replies). If candidate is a plain
        # message (HumanMessage, text-only AIMessage, SystemMessage), the
        # split at j is safe.
        return j, False

    # Walked all the way to index 1 and every position from 1..midpoint is
    # a ToolMessage (so middle[0] is the AIMessage-with-tool_calls). No
    # interior safe boundary exists — fall back to unsafe halving.
    return midpoint, True


async def _chunk_summarize(
    *,
    middle_messages: list[BaseMessage],
    prior_summary: str,
    operation: str,
    summarizer_model_id: str,
    task_id: str,
    tenant_id: str,
    agent_id: str,
    checkpoint_id: str | None,
    cost_ledger: CostLedgerRepository,
    callbacks: list[BaseCallbackHandler] | None,
    summarized_through_turn_index_after: int | None,
    summarizer_context_window: int,
) -> SummarizeResult:
    """Recursive chunk-summariser for middles that exceed the summariser's
    effective context budget.

    The middle is split in halves at a safe boundary (walked back from the
    natural midpoint to avoid splitting an AIMessage-with-tool_calls from
    its ToolMessage replies). Each half is summarised independently with
    ``prior_summary=""`` — the top-level ``prior_summary`` is NEVER chunked
    and NEVER re-summarised recursively. A final concatenation call over
    the synthetic middle of per-chunk ``AIMessage`` summaries re-introduces
    the original ``prior_summary``.

    Cost-ledger rows:
    - Each leaf LLM call (fits-in-one-shot chunk summary) → one row tagged
      ``"compaction.tier3.chunk"``.
    - Inner concat calls that themselves needed further recursion → one
      row per their nested sub-calls, each tagged with the operation
      passed in at that recursion level.
    - The outermost final concat → one row tagged with the ``operation``
      argument of the top-level ``_chunk_summarize`` call
      (``"compaction.tier3"`` when invoked from ``summarize_slice``).

    Failure semantics:
    - If any chunk's retry loop exhausts with retryable errors → the
      top-level result is ``skipped=True, skipped_reason="retryable"``.
    - If any chunk fails fatally → ``skipped=True, skipped_reason="fatal"``.
    - In either case, NO synthetic / partial summary is returned. The
      pipeline's watermark-don't-advance behaviour preserves the strict-
      append invariant.
    """
    n = len(middle_messages)
    assert n >= 2, "caller guards empty-slice before dispatch"

    split, is_unsafe = _find_safe_split(middle_messages)
    # Progress guarantee: 0 < split < n (enforced by _find_safe_split + assert).
    assert 0 < split < n, (
        f"progress violation: split={split} len={n} — _find_safe_split bug"
    )

    if is_unsafe:
        _logger.warning(
            "compaction.tier3_unsafe_chunk_split",
            tenant_id=tenant_id,
            agent_id=agent_id,
            task_id=task_id,
            summarizer_model=summarizer_model_id,
            middle_len=n,
            split_index=split,
            reason=(
                "no interior safe boundary — middle is one AIMessage-with-"
                "tool_calls followed by ToolMessages. Unsafe halving at "
                "len//2 — tool-pair context preserved via summary text "
                "rather than structural boundary."
            ),
        )

    left_half = middle_messages[:split]
    right_half = middle_messages[split:]

    # ---------------------------------------------------------------- #
    # Summarise each half — recurse if still oversized, else one-shot.  #
    # prior_summary is STRIPPED on recursive calls per design contract. #
    # ---------------------------------------------------------------- #
    left_result = await _summarize_chunk_half(
        half=left_half,
        summarizer_model_id=summarizer_model_id,
        task_id=task_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        checkpoint_id=checkpoint_id,
        cost_ledger=cost_ledger,
        callbacks=callbacks,
        summarized_through_turn_index_after=summarized_through_turn_index_after,
        summarizer_context_window=summarizer_context_window,
    )
    if left_result.skipped:
        # Strict-append: propagate skip; NO partial summary written.
        return _zero_out_cost_on_skip(left_result)

    right_result = await _summarize_chunk_half(
        half=right_half,
        summarizer_model_id=summarizer_model_id,
        task_id=task_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        checkpoint_id=checkpoint_id,
        cost_ledger=cost_ledger,
        callbacks=callbacks,
        summarized_through_turn_index_after=summarized_through_turn_index_after,
        summarizer_context_window=summarizer_context_window,
    )
    if right_result.skipped:
        return _zero_out_cost_on_skip(right_result)

    # ---------------------------------------------------------------- #
    # Final concat call — synthetic middle of chunk summaries,         #
    # re-introducing the original prior_summary. Tagged with the       #
    # incoming `operation`.                                             #
    # ---------------------------------------------------------------- #
    synthetic_middle: list[BaseMessage] = [
        AIMessage(content=left_result.summary_text or ""),
        AIMessage(content=right_result.summary_text or ""),
    ]

    # The synthetic middle is 2 `AIMessage(chunk_summary)` entries. Chunk
    # summaries are bounded by the summariser prompt (≤ ~400 words each)
    # AND by SUMMARIZER_MAX_OUTPUT_TOKENS, so a concat payload of
    # `prior_summary + 2*summary_text` is effectively always within budget
    # on any sanely-sized summariser window. We single-call it
    # unconditionally rather than recurse — recursing on a 2-message
    # synthetic middle would only ever split at index 1 (single-message
    # halves), a degenerate shape that produces no progress and risks a
    # recursion loop under pathological headroom / window configurations.
    # If `prior_summary` is ALSO oversized, that is the caller's bug (the
    # summariser cannot both accept and produce text larger than its own
    # context window); we still attempt the call so the provider's error
    # surfaces cleanly through the retry loop rather than vanishing into
    # silent recursion.
    final_result = await _summarize_single_call(
        slice_messages=synthetic_middle,
        prior_summary=prior_summary,
        operation=operation,
        summarizer_model_id=summarizer_model_id,
        task_id=task_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        checkpoint_id=checkpoint_id,
        cost_ledger=cost_ledger,
        callbacks=callbacks,
        summarized_through_turn_index_after=summarized_through_turn_index_after,
    )

    if final_result.skipped:
        return _zero_out_cost_on_skip(final_result)

    # Accumulate tokens / cost / latency across all sub-calls.
    return SummarizeResult(
        summary_text=final_result.summary_text,
        skipped=False,
        skipped_reason=None,
        summarizer_model_id=summarizer_model_id,
        tokens_in=left_result.tokens_in + right_result.tokens_in + final_result.tokens_in,
        tokens_out=left_result.tokens_out + right_result.tokens_out + final_result.tokens_out,
        cost_microdollars=(
            left_result.cost_microdollars
            + right_result.cost_microdollars
            + final_result.cost_microdollars
        ),
        latency_ms=(
            left_result.latency_ms + right_result.latency_ms + final_result.latency_ms
        ),
    )


async def _summarize_chunk_half(
    *,
    half: list[BaseMessage],
    summarizer_model_id: str,
    task_id: str,
    tenant_id: str,
    agent_id: str,
    checkpoint_id: str | None,
    cost_ledger: CostLedgerRepository,
    callbacks: list[BaseCallbackHandler] | None,
    summarized_through_turn_index_after: int | None,
    summarizer_context_window: int,
) -> SummarizeResult:
    """Summarise one half of a chunked middle.

    Always uses the chunk operation tag ``"compaction.tier3.chunk"`` and
    ``prior_summary=""`` — only the outermost concat carries the original
    prior_summary.

    If the half still doesn't fit in the summariser window, recurses into
    :func:`_chunk_summarize` with the chunk operation tag (so EVERY
    LLM-call row inside this half, including its inner concat, is
    attributed as ``compaction.tier3.chunk``).
    """
    if len(half) < 2:
        # A 1-message half with content we cannot safely drop: pass it
        # through the single-call path so the LLM still produces a summary
        # string. format_messages_for_summary handles any message type.
        # (Safe-boundary walk-back guards against this in practice — the
        # unsafe fallback splits at len//2 which is >= 1.)
        if not half:
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
        # 1-message half: just wrap in a degenerate synthetic middle that
        # format_messages_for_summary can render without a type-check
        # failure. We still count this as a "chunk" row.
        return await _summarize_single_call(
            slice_messages=[half[0], half[0]],
            prior_summary="",
            operation="compaction.tier3.chunk",
            summarizer_model_id=summarizer_model_id,
            task_id=task_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            checkpoint_id=checkpoint_id,
            cost_ledger=cost_ledger,
            callbacks=callbacks,
            summarized_through_turn_index_after=summarized_through_turn_index_after,
        )

    if _payload_fits(
        slice_messages=half,
        prior_summary="",
        summarizer_context_window=summarizer_context_window,
    ):
        return await _summarize_single_call(
            slice_messages=half,
            prior_summary="",
            operation="compaction.tier3.chunk",
            summarizer_model_id=summarizer_model_id,
            task_id=task_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            checkpoint_id=checkpoint_id,
            cost_ledger=cost_ledger,
            callbacks=callbacks,
            summarized_through_turn_index_after=summarized_through_turn_index_after,
        )

    return await _chunk_summarize(
        middle_messages=half,
        prior_summary="",
        operation="compaction.tier3.chunk",
        summarizer_model_id=summarizer_model_id,
        task_id=task_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        checkpoint_id=checkpoint_id,
        cost_ledger=cost_ledger,
        callbacks=callbacks,
        summarized_through_turn_index_after=summarized_through_turn_index_after,
        summarizer_context_window=summarizer_context_window,
    )


def _zero_out_cost_on_skip(result: SummarizeResult) -> SummarizeResult:
    """Return a skipped SummarizeResult with summary_text cleared.

    Ensures the top-level caller sees a consistent shape on propagation
    from a failing sub-call: ``summary_text=None`` so the pipeline cannot
    accidentally persist a partial chunk summary when another chunk
    failed.
    """
    if not result.skipped:
        return result
    return SummarizeResult(
        summary_text=None,
        skipped=True,
        skipped_reason=result.skipped_reason,
        summarizer_model_id=result.summarizer_model_id,
        tokens_in=0,
        tokens_out=0,
        cost_microdollars=0,
        latency_ms=0,
    )


def _extract_text_content_from_response(content: Any) -> str:
    """Flatten block-list content from an LLM response to plain string."""
    return _extract_content_from_value(content)
