"""Phase 2 Track 5 — ``memory_write`` LangGraph node.

The state schema has been moved to :mod:`executor.compaction.state` as
:class:`RuntimeState` (Track 7 Task 2 refactor).  All task graphs now use ``RuntimeState`` regardless of
whether the memory stack is enabled.

``observations`` is an append-only list reduced by ``operator.add``.
``pending_memory`` is written by the ``memory_write`` node on the agent's
terminal branch and read once by the worker's post-``astream`` commit path.

The node itself is intentionally factored so that the heavy external calls
(summarizer LLM and embedding provider) are injected as plain async callables.
That makes the node unit-testable without a live provider or DB, and keeps the
contract small enough that the wiring in :mod:`executor.graph` stays readable.

Terminal-only guarantee
-----------------------
This module provides the node function. The graph assembly in
:mod:`executor.graph` wires it only into the "no pending tool calls" branch —
the **single** terminal path out of the ``agent`` node. HITL pauses, budget
pauses, cancellations, and dead-letters all exit via different paths and
therefore never traverse ``memory_write``. That invariant is enforced by the
edge wiring, not by anything inside this node. See
``docs/design-docs/phase-2/track-5-memory.md`` § "Successful-task memory
write — hybrid graph-node + worker commit".
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from executor.compaction.state import RuntimeState
from executor.embeddings import EmbeddingResult, compute_embedding

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants (documented in services/worker-service/README.md)
# ---------------------------------------------------------------------------

# The env variable is consulted once at import time. Operators override the
# platform default via ``MEMORY_DEFAULT_SUMMARIZER_MODEL`` — the compiled-in
# fallback is a cheap Haiku-class model per the design doc. We read env at
# import time (not per call) so test monkeypatching needs to reload the
# module; the production worker is long-lived so re-reading would be waste.
PLATFORM_DEFAULT_SUMMARIZER_MODEL: str = os.environ.get(
    "MEMORY_DEFAULT_SUMMARIZER_MODEL", "claude-haiku-4-5"
)

# Sentinel values written into ``agent_memory_entries.summarizer_model_id``
# when the summarizer is unavailable. Both sentinels fall outside the normal
# ``<provider>/<model>`` shape, so the column lives free-form (no FK to
# ``models``).
SUMMARIZER_TEMPLATE_FALLBACK = "template:fallback"

# Dead-letter sentinel (Task 8). Template-only write on genuine-failure
# dead-letter with observations.
SUMMARIZER_TEMPLATE_DEAD_LETTER = "template:dead_letter"

# Dead-letter reason constants reused across worker code. Keep these strings
# in sync with ValidationConstants.ALLOWED_DEAD_LETTER_REASONS (Java) and the
# latest dead_letter_reason migration in infrastructure/database/migrations/.
DEAD_LETTER_REASON_CANCELLED_BY_USER = "cancelled_by_user"

# Track 7 — Context Window Management hard-floor safety net.
# Emitted when Tier 1 + 1.5 + 3 compaction together cannot reduce estimated
# input tokens below the model's context window.
# Migration: 0015_context_exceeded_dead_letter_reason.sql
DEAD_LETTER_REASON_CONTEXT_EXCEEDED_IRRECOVERABLE = "context_exceeded_irrecoverable"

# Attached-memory prompt-prefix caps (Task 8). Observations and summary
# are injected into the initial prompt; we cap per-block byte sizes so a
# pathological customer attach does not blow the context window.
_ATTACH_INJECTION_TITLE_CAP = 200
_ATTACH_INJECTION_SUMMARY_CAP = 4000
_ATTACH_INJECTION_OBSERVATION_CAP = 2000

# Caps documented in the design doc § "Successful-task memory write".
_TITLE_INPUT_SLICE_CHARS = 80
_SUMMARY_FINAL_OUTPUT_SLICE_CHARS = 1024

# The node name used by the budget carve-out lookup in ``graph.py``. Anything
# that imports this should NOT hard-code the string.
MEMORY_WRITE_NODE_NAME = "memory_write"

# ``tags`` is reserved in the schema for forward-compatibility (v1 has no tag
# tool or tag input). The node always writes an empty list. Callers MUST NOT
# invent tags at write time — that is deferred to a future iteration.
_EMPTY_TAGS: list[str] = []


# ---------------------------------------------------------------------------
# RuntimeState re-export — canonical location is executor.compaction.state
# ---------------------------------------------------------------------------

# Re-exported for the benefit of modules that imported the state type from
# this module before the Track 7 Task 2 refactor.  New code should import
# directly from executor.compaction.state.
__all__ = [
    "RuntimeState",
]


# ---------------------------------------------------------------------------
# Effective-memory gate — Task 12 rewrite
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryDecision:
    """Two-boolean result of :func:`effective_memory_decision`.

    * ``stack_enabled`` — gates the memory stack as a whole: observations
      channel, ``memory_note`` / ``save_memory`` tool registration, the
      attached-memory preamble, and the ``memory_write`` node registration.
      Identical to the pre-Task-12 ``effective_memory_enabled`` bool.
    * ``auto_write`` — when ``True``, the terminal ``agent → memory_write``
      edge fires unconditionally (today's ``always`` behaviour). When
      ``False`` (``agent_decides`` mode), routing inspects ``memory_opt_in``
      at runtime and only traverses ``memory_write`` if the agent opted in.
    """

    stack_enabled: bool
    auto_write: bool


def effective_memory_decision(
    *,
    agent_config: dict[str, Any],
    memory_mode: str,
) -> MemoryDecision:
    """Single gate used by every downstream memory branch (Task 12).

    Mapping (see task spec "Shared Contract"):

    * ``enabled=False OR mode=skip`` → ``MemoryDecision(False, False)`` —
      identical to today's memory-disabled path.
    * ``enabled=True AND mode=always`` → ``MemoryDecision(True, True)`` —
      current memory-enabled behaviour.
    * ``enabled=True AND mode=agent_decides`` →
      ``MemoryDecision(True, False)`` — memory stack on, ``memory_write``
      routing gated at runtime by ``memory_opt_in``.

    An unrecognised ``memory_mode`` value is treated as ``skip`` so a mis-
    serialized payload never silently writes a memory the customer didn't
    ask for.
    """

    memory = agent_config.get("memory") if isinstance(agent_config, dict) else None
    if not isinstance(memory, dict):
        return MemoryDecision(stack_enabled=False, auto_write=False)
    enabled = memory.get("enabled", False)
    if not isinstance(enabled, bool) or not enabled:
        return MemoryDecision(stack_enabled=False, auto_write=False)
    mode = memory_mode if isinstance(memory_mode, str) else ""
    if mode == "always":
        return MemoryDecision(stack_enabled=True, auto_write=True)
    if mode == "agent_decides":
        return MemoryDecision(stack_enabled=True, auto_write=False)
    # "skip" and any other value collapse to the disabled-stack result.
    return MemoryDecision(stack_enabled=False, auto_write=False)


# ---------------------------------------------------------------------------
# Summarizer contract (callable signature documented below)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SummarizerResult:
    """What the summarizer callable returns to the node.

    The node does not own the LLM client — :mod:`executor.graph` builds a
    closure that calls the configured chat model and returns this dataclass.
    Keeping the shape here decouples the node's unit tests from the chat
    client and makes the cost-attribution contract explicit.
    """

    title: str
    summary: str
    model_id: str
    tokens_in: int
    tokens_out: int
    cost_microdollars: int


class SummarizerCallable(Protocol):
    """Injected callable signature. Implemented by :mod:`executor.graph`."""

    async def __call__(
        self, *, system: str, user: str, model_id: str
    ) -> SummarizerResult | Any:
        ...


EmbeddingCallable = Callable[[str], Awaitable[EmbeddingResult | None]]


# ---------------------------------------------------------------------------
# Template fallback helper
# ---------------------------------------------------------------------------


def build_pending_memory_template_fallback(
    *,
    task_input: str | None,
    final_output: str | None,
    observations: list[str],
    commit_rationales: list[str] | None = None,
) -> dict[str, Any]:
    """Build a ``pending_memory`` dict when the summarizer is unavailable.

    Invariant preserved across all paths: every completed memory-enabled task
    emits exactly one ``agent_memory_entries`` row. The caller turns this
    dict into the row verbatim; ``summarizer_model_id`` is the sentinel
    ``'template:fallback'`` so operators can later regenerate.

    ``commit_rationales`` is accepted but not folded into the fallback
    summary text — kept verbatim in the snapshot for later regeneration.
    """

    safe_input = task_input or ""
    input_slice = _sanitize_one_line(safe_input)[:_TITLE_INPUT_SLICE_CHARS]
    title = f"Completed: {input_slice}"

    safe_output = final_output if isinstance(final_output, str) else ""
    output_slice = safe_output[:_SUMMARY_FINAL_OUTPUT_SLICE_CHARS]
    summary = (
        f"{output_slice} "
        "[summary generation unavailable; review observations and linked "
        "task trace for detail.]"
    ).strip()

    return {
        "title": title,
        "summary": summary,
        "outcome": "succeeded",
        "content_vec": None,  # Caller may overwrite with the embedding result.
        "summarizer_model_id": SUMMARIZER_TEMPLATE_FALLBACK,
        "observations_snapshot": list(observations),
        "commit_rationales_snapshot": list(commit_rationales or []),
        "tags": list(_EMPTY_TAGS),
        # Ledger-attribution metadata. The fallback never produced a
        # billable summarizer call, so all three are zero.
        "summarizer_tokens_in": 0,
        "summarizer_tokens_out": 0,
        "summarizer_cost_microdollars": 0,
    }


def _sanitize_one_line(text: str) -> str:
    """Collapse whitespace so the title renders cleanly in Console lists."""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# memory_write node
# ---------------------------------------------------------------------------


def _extract_final_output(messages: list[BaseMessage]) -> str | None:
    """Best-effort recovery of the agent's final answer for the template
    fallback. Used only when the summarizer failed — the happy path never
    reads this text.
    """
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                joined = "\n".join(p for p in parts if p)
                if joined.strip():
                    return joined
    return None


def _build_summarizer_prompt(
    *,
    messages: list[BaseMessage],
    observations: list[str],
    commit_rationales: list[str] | None = None,
) -> tuple[str, str]:
    """Assemble system + user messages for the summarizer call.

    * ``system`` — describes the exact output shape. Keeping this out of the
      user message lets template-fallback stay layout-agnostic.
    * ``user`` — the per-task payload: findings (observations), commit
      rationales, and the truncated transcript. Findings answer "what did
      the agent learn"; rationales answer "why did the agent decide this
      run was worth saving" — distinct concepts, rendered as distinct
      sections since issue #102.
    """
    system = (
        "You are the post-task memory summarizer. Produce a concise, "
        "retrospective memory entry for the task that just completed. "
        "Output exactly two sections, separated by a single blank line:\n"
        "TITLE: <one line, action-oriented, max 10 words>\n"
        "SUMMARY: <one paragraph, ≤400 words, describing what happened and "
        "why — lean on the agent findings below; treat commit rationale(s) "
        "as justification context, not content to repeat verbatim>"
    )
    obs_block = (
        "AGENT FINDINGS:\n"
        + "\n".join(f"- {o}" for o in observations)
        if observations
        else "AGENT FINDINGS: (none)"
    )
    rationales = list(commit_rationales or [])
    rationale_block = (
        "COMMIT RATIONALE:\n"
        + "\n".join(f"- {r}" for r in rationales)
        if rationales
        else "COMMIT RATIONALE: (none)"
    )
    # Keep the transcript summary bounded — very long runs can otherwise
    # blow the summarizer's context. We hand the assistant a compact
    # representation; the checkpointer still has the full trace.
    transcript_lines: list[str] = []
    for msg in messages[-40:]:  # last ~40 messages is plenty for a summary.
        role = getattr(msg, "type", msg.__class__.__name__).upper()
        content = msg.content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            content_str = "\n".join(parts)
        else:
            content_str = str(content)
        transcript_lines.append(f"{role}: {content_str[:2000]}")
    user = (
        obs_block
        + "\n\n"
        + rationale_block
        + "\n\nTRANSCRIPT:\n"
        + "\n".join(transcript_lines)
    )
    return system, user


def _coerce_summarizer_result(raw: Any, model_id: str) -> SummarizerResult:
    """Accept either a :class:`SummarizerResult` or a duck-typed namespace
    with the same attributes. Callers in production return the dataclass;
    tests occasionally use :class:`types.SimpleNamespace` for brevity.
    """
    if isinstance(raw, SummarizerResult):
        return raw
    return SummarizerResult(
        title=getattr(raw, "title", ""),
        summary=getattr(raw, "summary", ""),
        model_id=getattr(raw, "model_id", model_id),
        tokens_in=int(getattr(raw, "tokens_in", 0) or 0),
        tokens_out=int(getattr(raw, "tokens_out", 0) or 0),
        cost_microdollars=int(getattr(raw, "cost_microdollars", 0) or 0),
    )


async def memory_write_node(
    state: dict[str, Any],
    *,
    task_input: str | None,
    summarizer_model_id: str | None,
    summarizer_callable: SummarizerCallable,
    embedding_callable: EmbeddingCallable | None = None,
    tenant_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    config: RunnableConfig | None = None,
) -> Command:
    """LangGraph node. Reads ``messages`` + ``observations`` off ``state``,
    calls the summarizer LLM and the embedding provider, and returns a
    :class:`Command` updating ``pending_memory``.

    The node itself does no DB writes — the worker's post-``astream`` commit
    path owns the transaction. That split is deliberate: keeping the LLM call
    inside a LangGraph node means the checkpointer absorbs mid-call crashes
    and a re-entered node sees a fresh retry path.

    Parameters mirror the design-doc contract:

    - ``summarizer_model_id`` — from ``agent_config.memory.summarizer_model``;
      falls back to :data:`PLATFORM_DEFAULT_SUMMARIZER_MODEL` when ``None``.
    - ``summarizer_callable`` / ``embedding_callable`` — injected by
      :mod:`executor.graph`. ``embedding_callable`` defaults to
      :func:`executor.embeddings.compute_embedding` in tests where the caller
      is willing to make a real network call; unit tests override it.
    """
    del config  # Unused for now; kept in the signature for forward compat.

    started_ns = time.monotonic_ns()
    messages = state.get("messages") or []
    observations = list(state.get("observations") or [])
    # Issue #102 — commit_rationales is the new parallel channel for
    # save_memory/commit_memory reasons. Kept separate from observations so
    # the detail UI and the summarizer prompt can render the two concepts
    # distinctly. Older tasks (pre-migration-0023) may have no field on
    # state → fall back to empty list.
    commit_rationales = list(state.get("commit_rationales") or [])
    resolved_model_id = summarizer_model_id or PLATFORM_DEFAULT_SUMMARIZER_MODEL
    summarizer_cost_microdollars = 0
    summarizer_tokens_in = 0
    summarizer_tokens_out = 0

    logger.info(
        "memory.write.started tenant_id=%s agent_id=%s task_id=%s "
        "observations=%d commit_rationales=%d messages=%d",
        tenant_id, agent_id, task_id,
        len(observations), len(commit_rationales), len(messages),
    )

    # 1. Summarizer call — template fallback on any unexpected exception.
    try:
        system, user = _build_summarizer_prompt(
            messages=messages,
            observations=observations,
            commit_rationales=commit_rationales,
        )
        raw = await summarizer_callable(
            system=system, user=user, model_id=resolved_model_id
        )
        summarizer = _coerce_summarizer_result(raw, resolved_model_id)
        if not summarizer.title or not summarizer.summary:
            raise RuntimeError(
                "summarizer returned empty title/summary; treating as outage"
            )
        pending_memory: dict[str, Any] = {
            "title": summarizer.title,
            "summary": summarizer.summary,
            "outcome": "succeeded",
            "content_vec": None,  # filled in step 2.
            "summarizer_model_id": summarizer.model_id or resolved_model_id,
            "observations_snapshot": observations,
            "commit_rationales_snapshot": commit_rationales,
            "tags": list(_EMPTY_TAGS),
            "summarizer_tokens_in": summarizer.tokens_in,
            "summarizer_tokens_out": summarizer.tokens_out,
            "summarizer_cost_microdollars": summarizer.cost_microdollars,
        }
        summarizer_cost_microdollars = summarizer.cost_microdollars
        summarizer_tokens_in = summarizer.tokens_in
        summarizer_tokens_out = summarizer.tokens_out
    except Exception as exc:
        logger.warning(
            "memory.write.summarizer_failed tenant_id=%s agent_id=%s "
            "task_id=%s error_class=%s error_message=%s",
            tenant_id, agent_id, task_id,
            type(exc).__name__, _short(str(exc)),
        )
        final_output = _extract_final_output(messages)
        pending_memory = build_pending_memory_template_fallback(
            task_input=task_input,
            final_output=final_output,
            observations=observations,
            commit_rationales=commit_rationales,
        )

    # 2. Embedding — compute over the concatenated title + summary + obs + tags.
    embed_text = _build_embedding_text(pending_memory)
    embed_callable = embedding_callable or compute_embedding
    try:
        embed_result = await embed_callable(embed_text)
    except Exception as exc:
        # compute_embedding itself never raises, but injected test doubles
        # might. Keep the node bulletproof.
        logger.warning(
            "memory.write.embedding_unexpected_exception tenant_id=%s "
            "agent_id=%s task_id=%s error=%s",
            tenant_id, agent_id, task_id, _short(str(exc)),
        )
        embed_result = None

    if embed_result is None:
        pending_memory["content_vec"] = None
        pending_memory["embedding_tokens"] = 0
        pending_memory["embedding_cost_microdollars"] = 0
        logger.info(
            "memory.write.embedding_deferred tenant_id=%s agent_id=%s task_id=%s",
            tenant_id, agent_id, task_id,
        )
    else:
        pending_memory["content_vec"] = list(embed_result.vector)
        pending_memory["embedding_tokens"] = embed_result.tokens
        pending_memory["embedding_cost_microdollars"] = (
            embed_result.cost_microdollars
        )

    latency_ms = (time.monotonic_ns() - started_ns) // 1_000_000
    logger.info(
        "memory.write.node_completed tenant_id=%s agent_id=%s task_id=%s "
        "latency_ms=%d summarizer_model_id=%s content_vec_null=%s "
        "summarizer_cost_microdollars=%d summarizer_tokens_in=%d "
        "summarizer_tokens_out=%d",
        tenant_id, agent_id, task_id, latency_ms,
        pending_memory["summarizer_model_id"],
        pending_memory["content_vec"] is None,
        summarizer_cost_microdollars,
        summarizer_tokens_in,
        summarizer_tokens_out,
    )

    return Command(update={"pending_memory": pending_memory})


def _build_embedding_text(pending_memory: dict[str, Any]) -> str:
    """Concatenate the text that seeds the content_vec — matches the
    generated-column expression so search-time BM25 and embedding-time
    vector share a single content surface.
    """
    parts = [
        pending_memory.get("title") or "",
        pending_memory.get("summary") or "",
        " ".join(pending_memory.get("observations_snapshot") or []),
        " ".join(pending_memory.get("tags") or []),
    ]
    return " ".join(p for p in parts if p).strip()


def _short(message: str, limit: int = 200) -> str:
    cleaned = " ".join(message.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "..."


# ---------------------------------------------------------------------------
# Phase 2 Track 5 Task 8 — dead-letter template, attached-memory preamble,
# and first-execution predicate.
# ---------------------------------------------------------------------------


# Dead-letter template title caps the first slice of task_input at 50 chars
# (design doc § "Dead-letter hook (failed tasks with observations)").
_DEAD_LETTER_TITLE_INPUT_SLICE_CHARS = 50


def build_pending_memory_dead_letter_template(
    *,
    task_input: str | None,
    observations: list[str],
    retry_count: int | None,
    last_error_code: str | None,
    last_error_message: str | None,
    commit_rationales: list[str] | None = None,
) -> dict[str, Any]:
    """Build a ``pending_memory``-shaped dict for the dead-letter write path.

    The caller feeds this into :func:`core.memory_repository.upsert_memory_entry`
    the same way the successful-path template fallback does. The two templates
    differ in three places: ``outcome='failed'``, ``summarizer_model_id=
    'template:dead_letter'``, and the summary is an error-oriented one-liner.

    No LLM is invoked on this path — template only. Observations and commit
    rationales are preserved verbatim; the caller still owns the embedding
    call if it wants one.
    """
    safe_input = task_input or ""
    input_slice = _sanitize_one_line(safe_input)[
        :_DEAD_LETTER_TITLE_INPUT_SLICE_CHARS
    ]
    title = f"[Failed] {input_slice}" if input_slice else "[Failed]"

    retries = int(retry_count or 0)
    code = last_error_code or "unknown_error"
    msg = _short(last_error_message or "", limit=500)
    if msg:
        summary = (
            f"Task dead-lettered after {retries} retries: {code} — {msg}"
        )
    else:
        summary = f"Task dead-lettered after {retries} retries: {code}"

    return {
        "title": title,
        "summary": summary,
        "outcome": "failed",
        "content_vec": None,  # Caller overwrites with embedding or leaves NULL.
        "summarizer_model_id": SUMMARIZER_TEMPLATE_DEAD_LETTER,
        "observations_snapshot": list(observations),
        "commit_rationales_snapshot": list(commit_rationales or []),
        "tags": list(_EMPTY_TAGS),
        # No LLM call on this path — billable summarizer cost is zero.
        "summarizer_tokens_in": 0,
        "summarizer_tokens_out": 0,
        "summarizer_cost_microdollars": 0,
    }


def build_attached_memories_preamble(
    resolved_entries: list[dict[str, Any]],
) -> str | None:
    """Render a list of resolved attached-memory entries as a preamble block.

    Each entry is formatted per the design doc § "Attached-memory injection":

        [Attached memory: <title>]
        Observations:
        - <obs 1>
        - <obs 2>
        Summary: <summary>

    Blocks are concatenated in the caller-provided list order (which must
    already be sorted by ``position``). Returns ``None`` when the list is
    empty so the caller can skip prepending a ``SystemMessage`` entirely —
    Tasks with no attachments leave the initial message list unchanged.

    Per-block byte caps keep a pathological attach from blowing out the
    context window:
    * title capped at 200 chars
    * summary capped at ~4 KB
    * each observation capped at ~2 KB
    """
    if not resolved_entries:
        return None

    blocks: list[str] = []
    for entry in resolved_entries:
        title = (entry.get("title") or "")[:_ATTACH_INJECTION_TITLE_CAP]
        summary_text = (entry.get("summary") or "")[
            :_ATTACH_INJECTION_SUMMARY_CAP
        ]
        observations = entry.get("observations") or []

        lines: list[str] = [f"[Attached memory: {title}]"]
        if observations:
            lines.append("Observations:")
            for obs in observations:
                capped = (obs or "")[:_ATTACH_INJECTION_OBSERVATION_CAP]
                lines.append(f"- {capped}")
        else:
            lines.append("Observations: (none)")
        lines.append(f"Summary: {summary_text}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def checkpoint_tuple_has_prior_history(checkpoint_tuple: Any) -> bool:
    """Return ``True`` when the checkpoint already holds executed history.

    Shared predicate for the two Task 8 branches that must differ between a
    first execution and a follow-up / redrive / resume:

    * **Attached-memory injection** — only on first execution; the follow-up
      checkpoint already contains the previously-injected preamble.
    * **Follow-up seeding source** — always queries the memory row, but only
      first-execution paths are expected to find it empty.

    Predicate is deliberately loose: a tuple that returns ``None``, has no
    ``messages`` key in ``channel_values``, or whose messages list is empty
    is treated as "no history yet". Anything else is "has history".
    """
    if checkpoint_tuple is None:
        return False
    checkpoint = getattr(checkpoint_tuple, "checkpoint", None)
    if not isinstance(checkpoint, dict):
        return False
    values = checkpoint.get("channel_values")
    if not isinstance(values, dict):
        return False
    messages = values.get("messages")
    if not messages:
        return False
    # ``messages`` may legitimately be a non-empty list, tuple, or any other
    # truthy sequence — LangGraph serializers normalise to list, but we stay
    # tolerant of the channel-values shape.
    try:
        return len(messages) > 0
    except TypeError:
        return bool(messages)
