"""Track 7 Follow-up (Task 3) — ``pre_model_hook`` + replace-and-rehydrate projection.

This module is the new compaction entry point, replacing Track 7's
``compact_for_llm`` pipeline. It is shaped to match LangGraph's
``pre_model_hook`` protocol (invoked by ``create_react_agent`` or an
equivalent agent-loop before each LLM call) and returns the three-region
projection via ``llm_input_messages`` — a non-persistent message view that
LangGraph does NOT write back to ``state["messages"]``.

Three regions, assembled fresh each turn from the durable journal:

1. **System prompt** — always the first message (agent-config-provided).
2. **Summary region** — a single ``SystemMessage`` rendered from
   ``state["summary"]`` when non-empty, representing everything at indices
   ``[0 : summarized_through_turn_index]``.
3. **Middle region** — ``state["messages"][summarized_through : keep_window_start]``
   verbatim.
4. **Keep window** — ``state["messages"][keep_window_start : ]`` — positional
   slice covering the most-recent ``KEEP_TOOL_USES`` tool uses, orphan-aligned
   so the slice begins at an ``AIMessage`` with ``tool_calls`` whenever one
   precedes the walkback boundary.

When the projection's estimated token count exceeds
``COMPACTION_TRIGGER_FRACTION * model_context_window``, the hook fires
``summarize_slice`` against the RAW middle (never stubbed, never arg-truncated)
with the prior ``state["summary"]`` carried in. On success it emits
``summary`` + ``summarized_through_turn_index`` updates and rebuilds the
projection for the current turn with the fresh summary and an empty middle.

Invariants preserved from Track 7:

* ``state["messages"]`` is never mutated by this hook. Option C's recalled-
  reference replacement (Task 5) is the single sanctioned write and is called
  from Task 5's code path — we leave a documented hook-point here.
* Tool-use / tool-result pairing is preserved by the orphan-alignment
  walkback. No ``ToolMessage`` ever appears as the first non-``SystemMessage``
  of the projection.
* Shape validator passes on every projection.
* The pre-summarisation memory flush (Track 7 Task 9) fires inside the
  summarise branch with identical semantics (one-shot per task, requires
  memory-enabled + ``context_management.pre_tier3_memory_flush`` = True,
  skipped on heartbeat turns).
* The context-exceeded dead-letter path (Track 7 Task 10) is still the final
  safety valve: ``HardFloorEvent`` is emitted when, even after summarisation,
  the projection still exceeds ``model_context_window``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

from executor.compaction.defaults import (
    COMPACTION_TRIGGER_FRACTION,
    KEEP_TOOL_USES,
    TIER_3_MAX_FIRINGS_PER_TASK,
    get_platform_default_summarizer_model,
)


# ---------------------------------------------------------------------------
# Option C — reference-replacement for recalled ToolMessages absorbed into
# ``summary``. Exported so Task 5's tests can exercise it directly without
# running the full hook.
# ---------------------------------------------------------------------------


def _is_recalled_tool_message(msg: BaseMessage) -> bool:
    """Return True when ``msg`` is a ``recall_tool_result`` output."""
    if not isinstance(msg, ToolMessage):
        return False
    kwargs = getattr(msg, "additional_kwargs", None) or {}
    return bool(kwargs.get("recalled"))


def _reference_placeholder(original_tool_call_id: str) -> str:
    """Return the canonical Option C reference string.

    Shared between :func:`option_c_reference_replacement` and tests so the
    exact wording stays aligned. Keeping the original ``tool_call_id``
    inline lets the agent re-issue ``recall_tool_result`` to fetch the full
    content again if it still needs it post-summarisation.
    """
    return (
        f"[recalled content summarized; full content remains at original "
        f"tool_call_id='{original_tool_call_id}']"
    )


def option_c_reference_replacement(
    raw_messages: list[BaseMessage],
    *,
    previous_summarized_through: int,
    new_summarized_through: int,
) -> list[BaseMessage]:
    """Return replacement ``ToolMessage``s for any recalled messages
    absorbed into the new summary window.

    This is the ONE sanctioned mutation to ``state["messages"]`` under the
    replace-and-rehydrate architecture — everywhere else the durable journal
    is strictly append-only. The mutation is executed via LangGraph's
    ``add_messages`` reducer: we return ``ToolMessage`` instances whose
    ``id`` matches the original, and the reducer replaces them in place.

    Parameters
    ----------
    raw_messages:
        ``state["messages"]`` at entry to the compaction pass. Read-only.
    previous_summarized_through:
        The watermark BEFORE this compaction firing.
    new_summarized_through:
        The watermark after this firing — typically ``keep_window_start``.

    Returns
    -------
    list[BaseMessage]
        Zero or more replacement ``ToolMessage`` instances. An empty list
        means no recalled messages fell within the newly-summarised range
        (the common case — most tasks never call the recall tool).
    """
    if new_summarized_through <= previous_summarized_through:
        return []

    start = max(0, previous_summarized_through)
    end = min(len(raw_messages), new_summarized_through)
    if start >= end:
        return []

    replacements: list[BaseMessage] = []
    for i in range(start, end):
        msg = raw_messages[i]
        if not _is_recalled_tool_message(msg):
            continue
        existing_kwargs: dict[str, Any] = dict(
            getattr(msg, "additional_kwargs", None) or {}
        )
        # Idempotent: if this ToolMessage has already been reference-
        # replaced (a prior firing touched it), skip it so we don't
        # churn the id through the reducer again.
        if existing_kwargs.get("content_offloaded"):
            continue
        original_id = existing_kwargs.get("original_tool_call_id", "")
        existing_kwargs["content_offloaded"] = True
        replacement = ToolMessage(
            content=_reference_placeholder(original_id),
            tool_call_id=getattr(msg, "tool_call_id", "") or "",
            name=getattr(msg, "name", None),
            additional_kwargs=existing_kwargs,
        )
        # Preserve the message id so ``add_messages`` updates in place
        # rather than appending a duplicate entry.
        original_msg_id = getattr(msg, "id", None)
        if original_msg_id is not None:
            try:
                replacement.id = original_msg_id  # type: ignore[attr-defined]
            except Exception:
                # BaseModel.__setattr__ is permissive in LangChain; if a
                # future version tightens it, fall back to a model_copy so
                # we at least carry the id through.
                replacement = msg.model_copy(
                    update={
                        "content": replacement.content,
                        "additional_kwargs": existing_kwargs,
                    }
                )
        replacements.append(replacement)
    return replacements


# ---------------------------------------------------------------------------
# Projection rules for recalled ToolMessages (Task 5 §5, revised):
#   inside keep window → verbatim (already recalled for a reason)
#   outside keep window → STUBBED in place (envelope preserved, content short)
#
# We stub rather than drop because providers (Bedrock / Anthropic) require
# every ``tool_use`` block to be paired with its matching ``tool_result``.
# Dropping a recall response when the keep-window boundary falls between the
# invoking AIMessage and its ToolMessage leaves an orphan tool_use and the
# provider rejects the turn (observed on task 75f5a223 — dead-lettered with
# "Expected toolResult blocks at messages.N.content" from Bedrock Converse).
# The stub keeps the ToolMessage structurally valid and still prevents the
# re-offload / re-recall oscillation the original drop rule targeted, because
# the bytes are replaced with a short placeholder (not the full recalled
# content), so the projection does not grow turn-over-turn.
# ---------------------------------------------------------------------------


_OUT_OF_WINDOW_STUB_TEMPLATE = (
    "[recall response elided from older context; full content remains at "
    "original tool_call_id='{original}']"
)


def _stub_recalled_outside_keep_window(
    middle: list[BaseMessage],
) -> list[BaseMessage]:
    """Return ``middle`` with recalled ToolMessages' content stubbed.

    The ToolMessage envelope (role, ``tool_call_id``, ``name``,
    ``additional_kwargs``) is preserved verbatim so the invoking
    ``AIMessage.tool_calls`` entry still has its matching ``tool_result``
    and the provider accepts the request. Only ``.content`` is swapped for
    a short stub string pointing back at the original tool_call_id — the
    full recalled bytes remain in the artifact store and a fresh
    ``recall_tool_result`` call still returns them.

    Idempotent: re-stubbing a message that already carries the stub content
    is a no-op (no new instance allocated, no churn through reducers).
    """
    out: list[BaseMessage] = []
    for m in middle:
        if not _is_recalled_tool_message(m):
            out.append(m)
            continue
        kwargs = dict(getattr(m, "additional_kwargs", None) or {})
        original = kwargs.get("original_tool_call_id", "") or ""
        stub_content = _OUT_OF_WINDOW_STUB_TEMPLATE.format(original=original)
        current = getattr(m, "content", None)
        if isinstance(current, str) and current == stub_content:
            out.append(m)
            continue
        out.append(m.model_copy(update={"content": stub_content}))
    return out


# ---------------------------------------------------------------------------
# Event types — returned by the hook, emitted by the caller
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardFloorEvent:
    """Emitted when the projection exceeds ``model_context_window`` even after
    a summarisation attempt (or when summarisation was skipped).

    The caller must invoke the dead-letter path with
    ``reason=DEAD_LETTER_REASON_CONTEXT_EXCEEDED_IRRECOVERABLE``.
    """

    est_tokens: int
    model_context_window: int
    task_id: str = ""
    tenant_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class Tier3FiredEvent:
    """Emitted on a successful summarisation firing.

    ``new_summarized_through`` is the ``keep_window_start`` index for the
    firing turn — that is, the first message index that remains in the keep
    window and is NOT covered by the new summary.
    """

    summarizer_model_id: str
    tokens_in: int
    tokens_out: int
    new_summarized_through: int
    task_id: str = ""
    tenant_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class Tier3SkippedEvent:
    """Emitted when the summarisation trigger was met but the call was skipped.

    ``reason`` is one of: ``'retryable'`` | ``'fatal'`` | ``'cap_reached'`` |
    ``'empty_slice'``.
    """

    reason: str
    task_id: str = ""
    tenant_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class MemoryFlushFiredEvent:
    """Emitted once per task when the pre-summarisation memory flush fires.

    One-shot via ``memory_flush_fired_this_task``.
    """

    fired_at_step: int
    task_id: str = ""
    tenant_id: str = ""
    agent_id: str = ""


CompactionEvent = (
    HardFloorEvent | Tier3FiredEvent | Tier3SkippedEvent | MemoryFlushFiredEvent
)


# ---------------------------------------------------------------------------
# Pre-summarisation memory flush prompt + helpers (preserved from Track 7 Task 9)
# ---------------------------------------------------------------------------


_PRE_TIER3_FLUSH_PROMPT = (
    "You are about to have older context summarized. This is your one chance in\n"
    "this task to preserve cross-task-valuable facts. Call memory_note for anything\n"
    "you want to remember in future tasks — decisions, user preferences, non-obvious\n"
    "facts. If nothing qualifies, reply with an empty response."
)


def _is_heartbeat_turn(
    raw_messages: list[BaseMessage],
    last_super_step_message_count: int,
) -> bool:
    """True when no new messages have arrived since the last agent super-step."""
    return len(raw_messages) <= last_super_step_message_count


def should_fire_pre_tier3_flush(
    state: dict[str, Any],
    agent_config: dict[str, Any],
    raw_messages: list[BaseMessage],
) -> bool:
    """Return True iff the pre-summarisation memory flush should fire.

    All four conditions must hold:
      1. ``context_management.pre_tier3_memory_flush`` is True (default True).
      2. ``memory.enabled`` is True.
      3. ``memory_flush_fired_this_task`` is False (one-shot).
      4. NOT a heartbeat turn.
    """
    ctx_mgmt: dict[str, Any] = agent_config.get("context_management") or {}
    if not ctx_mgmt.get("pre_tier3_memory_flush", True):
        return False

    memory_cfg: dict[str, Any] = agent_config.get("memory") or {}
    if memory_cfg.get("enabled") is not True:
        return False

    if state.get("memory_flush_fired_this_task", False):
        return False

    last_count: int = state.get("last_super_step_message_count", 0) or 0
    if _is_heartbeat_turn(raw_messages, last_count):
        return False

    return True


# ---------------------------------------------------------------------------
# Keep-window computation — positional walkback with orphan alignment
# ---------------------------------------------------------------------------


def find_keep_window_start(
    raw_messages: list[BaseMessage],
    keep: int = KEEP_TOOL_USES,
) -> int:
    """Return the ``keep_window_start`` index for ``raw_messages``.

    Walk back from the end of ``raw_messages`` past the ``keep``-th-most-recent
    ``ToolMessage``, then align to the preceding ``AIMessage`` that carries
    ``tool_calls`` (orphan-prevention). When fewer than ``keep`` ToolMessages
    exist in total, return ``0`` — the whole message list is the keep window.

    The returned index always satisfies:
      * ``raw_messages[keep_window_start]`` is NOT a ``ToolMessage`` whose
        matching ``AIMessage`` lies below the index.
      * ``raw_messages[keep_window_start:]`` is a valid LLM-input slice
        modulo ``SystemMessage``-at-head handling (the hook prepends those
        separately).

    This helper extracts and shares the walkback logic that originally lived
    inside Track 7's ``compact_for_llm`` Tier 3 branch (post-PR #80
    regression fix in ``test_compaction_tier3_tool_boundary.py`` /
    ``test_compaction_tier3_second_firing_boundary.py``).
    """
    tool_positions = [
        i for i, m in enumerate(raw_messages) if isinstance(m, ToolMessage)
    ]
    if len(tool_positions) <= keep:
        # Every tool use is inside the keep window already.
        return 0

    start = tool_positions[-keep]

    # Orphan-prevention: if the walkback landed on a ToolMessage (always the
    # case above), step back to the nearest preceding AIMessage with
    # tool_calls so its matching tool_use is included verbatim.
    if isinstance(raw_messages[start], ToolMessage):
        for j in range(start - 1, -1, -1):
            candidate = raw_messages[j]
            if isinstance(candidate, AIMessage) and candidate.tool_calls:
                start = j
                break

    return start


# ---------------------------------------------------------------------------
# Projection builder
# ---------------------------------------------------------------------------


def _build_projection(
    *,
    system_prompt: str | None,
    platform_system_message: str | None,
    summary: str,
    middle: list[BaseMessage],
    keep_window: list[BaseMessage],
) -> list[BaseMessage]:
    """Assemble the three-region projection.

    Final shape: ``[SystemMessage(system_prompt), SystemMessage(platform_msg)?,
    SystemMessage(summary)?, *middle, *keep_window]``.

    The platform SystemMessage (auto-synthesised by the worker when
    memory is enabled) sits between the user's system prompt and the
    summary region to preserve today's behaviour — see ``agent_node`` in
    ``executor/graph.py``.
    """
    projection: list[BaseMessage] = []
    if system_prompt:
        projection.append(SystemMessage(content=system_prompt))
    if platform_system_message:
        projection.append(SystemMessage(content=platform_system_message))
    if summary:
        projection.append(
            SystemMessage(
                content=summary,
                additional_kwargs={"compaction": True},
            )
        )
    projection.extend(middle)
    projection.extend(keep_window)
    return projection


# ---------------------------------------------------------------------------
# Result type (analogous to Track 7's CompactionPassResult)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionPassResult:
    """Result of one ``compaction_pre_model_hook`` invocation.

    Attributes
    ----------
    messages:
        The three-region projection — the list passed to the LLM as
        ``llm_input_messages`` by the caller. Non-persistent; LangGraph does
        NOT write it back into ``state["messages"]``.
    state_updates:
        Dictionary of graph-state field updates. Always contains
        ``last_super_step_message_count``. On a firing turn also contains
        ``summary``, ``summarized_through_turn_index``, ``tier3_firings_count``.
    events:
        Structured-log events to emit.
    tier3_skipped:
        ``True`` when the summarisation trigger was met but the call was
        skipped (cap reached, fatal, retryable).
    """

    messages: list[BaseMessage]
    state_updates: dict[str, Any]
    events: list[CompactionEvent]
    tier3_skipped: bool = False


# ---------------------------------------------------------------------------
# Main entry point — ``pre_model_hook``-shaped
# ---------------------------------------------------------------------------


async def compaction_pre_model_hook(
    raw_messages: list[BaseMessage],
    state: dict[str, Any],
    agent_config: dict[str, Any],
    model_context_window: int,
    task_context: dict[str, Any],
    summarizer: Any,
    *,
    estimate_tokens_fn: Callable[[list[BaseMessage]], int],
    system_prompt: str | None = None,
    platform_system_message: str | None = None,
    summarizer_context_window: int | None = None,
) -> CompactionPassResult:
    """Track 7 Follow-up pre-model hook — replaces Track 7's ``compact_for_llm``.

    Parameters
    ----------
    raw_messages:
        ``state["messages"]`` verbatim. Treated as immutable — this function
        NEVER writes to the underlying list. Option C (Task 5) is the only
        sanctioned mutation and happens elsewhere.
    state:
        Read-only mapping view of the current graph state.
    agent_config:
        Agent configuration dict including ``context_management`` and
        ``memory`` sub-objects.
    model_context_window:
        Token budget of the main-agent model.
    task_context:
        Tenant/agent/task/checkpoint IDs + cost_ledger + callbacks.
    summarizer:
        Async callable shaped like :func:`summarize_slice` (Task 2). Must
        accept ``prior_summary`` and ``summarizer_context_window`` kwargs.
    estimate_tokens_fn:
        Callable ``(list[BaseMessage]) -> int`` — injected so tests supply a
        deterministic estimate without a real tokenizer.
    system_prompt:
        User-configured system prompt (agent config) — becomes the first
        message of every projection.
    platform_system_message:
        Platform-synthesised SystemMessage (e.g. attached-memories preamble).
        Appended after the system prompt and before the summary region.
    summarizer_context_window:
        Optional summariser model context window; forwarded to
        :func:`summarize_slice` so Task 2's recursive chunking engages when
        ``prior_summary + middle`` is too large for the summariser model.

    Returns
    -------
    CompactionPassResult
        Never raises. All errors are captured in events or the result object.
    """
    tenant_id: str = task_context.get("tenant_id", "")
    agent_id: str = task_context.get("agent_id", "")
    task_id: str = task_context.get("task_id", "")
    checkpoint_id: str | None = task_context.get("checkpoint_id")
    cost_ledger = task_context.get("cost_ledger")
    callbacks = task_context.get("callbacks") or []

    # Pull compaction-relevant state.
    summary: str = state.get("summary", "") or ""
    summarized_through: int = int(state.get("summarized_through_turn_index", 0) or 0)
    tier3_firings_count: int = int(state.get("tier3_firings_count", 0) or 0)
    tier3_fatal: bool = bool(state.get("tier3_fatal_short_circuited", False))

    # last_super_step_message_count tracks the length of the DURABLE message
    # list — NOT the length of any transient projection. We read from
    # state["messages"] so callers that inject transient system messages into
    # ``raw_messages`` cannot shift the heartbeat watermark by mistake.
    _persisted = state.get("messages")
    persisted_length = len(_persisted) if _persisted is not None else len(raw_messages)
    state_updates: dict[str, Any] = {
        "last_super_step_message_count": persisted_length,
    }
    events: list[CompactionEvent] = []
    tier3_skipped = False

    # Clamp summarized_through — legacy checkpoints may have a stale value.
    summarized_through = max(0, min(summarized_through, len(raw_messages)))

    # Defensive alignment: if the watermark lands on a ``ToolMessage`` (which
    # can happen on a legacy Track 7 checkpoint whose ``summarized_through``
    # was computed in the old, non-orphan-aligned index space), advance it
    # forward to the next index so the projection doesn't start with an
    # orphan ``ToolMessage``. The hook itself always writes an orphan-aligned
    # watermark when it fires, so this only fires during migration.
    while (
        summarized_through < len(raw_messages)
        and isinstance(raw_messages[summarized_through], ToolMessage)
    ):
        summarized_through += 1

    # Compute the three regions.
    keep_window_start = find_keep_window_start(raw_messages, keep=KEEP_TOOL_USES)
    # Safety: keep_window_start must be >= summarized_through. If the journal
    # shrank somehow (recovery edge case) or the watermark ran past the
    # computed keep-window start, treat middle as empty.
    if keep_window_start < summarized_through:
        keep_window_start = summarized_through

    middle = list(raw_messages[summarized_through:keep_window_start])
    keep_window = list(raw_messages[keep_window_start:])

    # Task 5 §5 (revised) — projection rule for recalled ToolMessages: keep
    # verbatim inside the keep window (the agent recalled them on purpose)
    # but STUB them in ``middle``. Stubbing, not dropping, preserves the
    # tool_use/tool_result pairing the provider requires; the short stub
    # still prevents the re-offload / re-recall oscillation the original
    # rule targeted because the bytes are replaced with a placeholder.
    # Once absorbed into ``summary``, the Option C replacement below keeps
    # the journal entry lossless (S3 still holds the original bytes).
    middle = _stub_recalled_outside_keep_window(middle)

    # Build the initial projection and estimate tokens.
    projection = _build_projection(
        system_prompt=system_prompt,
        platform_system_message=platform_system_message,
        summary=summary,
        middle=middle,
        keep_window=keep_window,
    )
    est_tokens = estimate_tokens_fn(projection)

    # Resolve context_management-owned summarizer model id and memory-flush
    # preconditions up front so we can decide whether to fire.
    ctx_mgmt: dict[str, Any] = agent_config.get("context_management") or {}

    # Summarisation trigger — DeepAgents-style fixed fraction of the main
    # model's context window.
    trigger_tokens = int(COMPACTION_TRIGGER_FRACTION * model_context_window)

    if est_tokens < trigger_tokens:
        # Below threshold — emit the projection as-is.
        # Still run the hard-floor safety net in case of pathological inputs.
        if est_tokens > model_context_window:
            events.append(
                HardFloorEvent(
                    est_tokens=est_tokens,
                    model_context_window=model_context_window,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                )
            )
        return CompactionPassResult(
            messages=projection,
            state_updates=state_updates,
            events=events,
            tier3_skipped=False,
        )

    # --------------------- Summarisation path ---------------------

    # Gate 1: fatal short-circuit (misconfigured summariser on a previous turn).
    if tier3_fatal:
        # Silent skip — no event, no cost. Still emit HardFloor when we can't
        # serve the projection.
        if est_tokens > model_context_window:
            events.append(
                HardFloorEvent(
                    est_tokens=est_tokens,
                    model_context_window=model_context_window,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                )
            )
        return CompactionPassResult(
            messages=projection,
            state_updates=state_updates,
            events=events,
            tier3_skipped=True,
        )

    # Gate 2: per-task firings cap.
    if tier3_firings_count >= TIER_3_MAX_FIRINGS_PER_TASK:
        events.append(
            Tier3SkippedEvent(
                reason="cap_reached",
                task_id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
            )
        )
        if est_tokens > model_context_window:
            events.append(
                HardFloorEvent(
                    est_tokens=est_tokens,
                    model_context_window=model_context_window,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                )
            )
        return CompactionPassResult(
            messages=projection,
            state_updates=state_updates,
            events=events,
            tier3_skipped=True,
        )

    # Pre-summarisation memory flush — one-shot, fires BEFORE the summariser
    # call on the first qualifying turn. The flush appends a SystemMessage to
    # the projection asking the agent to write any cross-task-valuable facts
    # to memory; the summariser call itself is deferred to the next turn.
    if should_fire_pre_tier3_flush(state, agent_config, raw_messages):
        flush_message = SystemMessage(
            content=_PRE_TIER3_FLUSH_PROMPT,
            additional_kwargs={
                "compaction": True,
                "compaction_event": "pre_tier3_memory_flush",
            },
        )
        state_updates["memory_flush_fired_this_task"] = True
        events.append(
            MemoryFlushFiredEvent(
                fired_at_step=len(raw_messages),
                task_id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
            )
        )
        flush_projection = [*projection, flush_message]
        est_tokens_with_flush = estimate_tokens_fn(flush_projection)
        if est_tokens_with_flush > model_context_window:
            events.append(
                HardFloorEvent(
                    est_tokens=est_tokens_with_flush,
                    model_context_window=model_context_window,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                )
            )
        return CompactionPassResult(
            messages=flush_projection,
            state_updates=state_updates,
            events=events,
            tier3_skipped=False,
        )

    # If middle is empty, there's nothing the summariser can reduce — skip.
    if not middle:
        events.append(
            Tier3SkippedEvent(
                reason="empty_slice",
                task_id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
            )
        )
        if est_tokens > model_context_window:
            events.append(
                HardFloorEvent(
                    est_tokens=est_tokens,
                    model_context_window=model_context_window,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                )
            )
        return CompactionPassResult(
            messages=projection,
            state_updates=state_updates,
            events=events,
            tier3_skipped=True,
        )

    # Resolve summariser model id.
    summarizer_model_id: str = (
        ctx_mgmt.get("summarizer_model")
        or get_platform_default_summarizer_model()
    )

    new_summarized_through = keep_window_start

    summarize_result = await summarizer(
        slice_messages=middle,
        summarizer_model_id=summarizer_model_id,
        task_id=task_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        checkpoint_id=checkpoint_id,
        cost_ledger=cost_ledger,
        callbacks=callbacks,
        summarized_through_turn_index_after=new_summarized_through,
        prior_summary=summary,
        summarizer_context_window=summarizer_context_window,
    )

    if summarize_result.skipped:
        # Fatal vs retryable.
        if summarize_result.skipped_reason == "fatal":
            state_updates["tier3_fatal_short_circuited"] = True
        events.append(
            Tier3SkippedEvent(
                reason=summarize_result.skipped_reason or "unknown",
                task_id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
            )
        )
        tier3_skipped = True

        # Hard-floor safety net.
        if est_tokens > model_context_window:
            events.append(
                HardFloorEvent(
                    est_tokens=est_tokens,
                    model_context_window=model_context_window,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                )
            )
        return CompactionPassResult(
            messages=projection,
            state_updates=state_updates,
            events=events,
            tier3_skipped=tier3_skipped,
        )

    # Success — replace summary + advance watermark + increment firings.
    new_summary = summarize_result.summary_text or ""
    state_updates["summary"] = new_summary
    state_updates["summarized_through_turn_index"] = new_summarized_through
    state_updates["tier3_firings_count"] = tier3_firings_count + 1

    # Task 5 §6 — Option C reference-replacement. This is the SOLE sanctioned
    # mutation to ``state["messages"]`` under the replace-and-rehydrate
    # architecture; every other code path treats the journal as append-only.
    # Recalled ``ToolMessage`` instances whose indices fall within the newly-
    # absorbed window get their content replaced with a short reference
    # string pointing back at the original ``tool_call_id`` — the full bytes
    # stay in S3 and a fresh ``recall_tool_result`` call still returns them.
    # LangGraph's ``add_messages`` reducer performs the replacement by id;
    # ``option_c_reference_replacement`` preserves the original message id on
    # every returned instance. The mutation is part of the SAME state update
    # as ``summary`` / ``summarized_through_turn_index`` so no intermediate
    # state is ever observed externally.
    _replacements = option_c_reference_replacement(
        raw_messages,
        previous_summarized_through=summarized_through,
        new_summarized_through=new_summarized_through,
    )
    if _replacements:
        state_updates["messages"] = _replacements

    events.append(
        Tier3FiredEvent(
            summarizer_model_id=summarize_result.summarizer_model_id,
            tokens_in=summarize_result.tokens_in,
            tokens_out=summarize_result.tokens_out,
            new_summarized_through=new_summarized_through,
            task_id=task_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
    )

    # Rebuild the projection for THIS turn with the new summary and empty
    # middle (everything below ``new_summarized_through`` is now absorbed
    # into ``summary``).
    post_projection = _build_projection(
        system_prompt=system_prompt,
        platform_system_message=platform_system_message,
        summary=new_summary,
        middle=[],
        keep_window=keep_window,
    )
    post_est = estimate_tokens_fn(post_projection)
    if post_est > model_context_window:
        events.append(
            HardFloorEvent(
                est_tokens=post_est,
                model_context_window=model_context_window,
                task_id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
            )
        )

    return CompactionPassResult(
        messages=post_projection,
        state_updates=state_updates,
        events=events,
        tier3_skipped=False,
    )
