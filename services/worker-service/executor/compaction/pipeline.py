"""Track 7 compaction pipeline — compact_for_llm entry point.

This module orchestrates the tiered compaction passes (Tier 1, Tier 1.5,
Tier 3) that run inside ``agent_node`` before every LLM call.  The pipeline
is **pure**: it never writes to the database directly (the Tier 3 summarizer
owns the cost-ledger row) and never mutates the input messages list.

Design constraints
------------------
* The pipeline returns a :class:`CompactionPassResult` containing the
  compacted message list, a ``state_updates`` dict to merge into graph state,
  and a list of structured-log events.  The caller (``agent_node``) is
  responsible for emitting those events so the pipeline itself has no logger
  coupling.
* The pipeline never dead-letters from within.  It emits :class:`HardFloorEvent`
  when all tiers together cannot bring estimated tokens below the model's
  context window; the caller invokes the existing dead-letter API.
* Tier 3 is guarded by two cost-safety gates:
  - ``tier3_firings_count >= TIER_3_MAX_FIRINGS_PER_TASK`` → skip and emit
    ``Tier3SkippedEvent(reason='cap_reached')``.
  - ``tier3_fatal_short_circuited = True`` → skip silently (fatal provider
    error on a prior call; re-attempting burns per-call cost forever).

See docs/design-docs/phase-2/track-7-context-window-management.md for the
full design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

from executor.compaction.defaults import (
    ARG_TRUNCATION_CAP_BYTES,
    KEEP_TOOL_USES,
    PLATFORM_EXCLUDE_TOOLS,
    TIER_3_MAX_FIRINGS_PER_TASK,
    TRUNCATABLE_TOOL_ARG_KEYS,
    get_platform_default_summarizer_model,
)
from executor.compaction.thresholds import resolve_thresholds
from executor.compaction.transforms import clear_tool_results, truncate_tool_call_args


# ---------------------------------------------------------------------------
# Event types — returned by the pipeline, emitted by the caller
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardFloorEvent:
    """Emitted when estimated tokens exceed the model context window after all tiers.

    The caller must invoke the dead-letter path with
    ``reason=DEAD_LETTER_REASON_CONTEXT_EXCEEDED_IRRECOVERABLE``.
    """

    est_tokens: int
    model_context_window: int
    task_id: str = ""
    tenant_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class Tier1AppliedEvent:
    """Emitted when Tier 1 (tool-result clearing) advanced the watermark."""

    messages_cleared: int
    est_tokens_saved: int
    new_watermark: int
    task_id: str = ""
    tenant_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class Tier15AppliedEvent:
    """Emitted when Tier 1.5 (arg truncation) advanced the watermark."""

    args_truncated: int
    bytes_saved: int
    new_watermark: int
    task_id: str = ""
    tenant_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class Tier3FiredEvent:
    """Emitted on a successful Tier 3 (summarization) firing."""

    summarizer_model_id: str
    tokens_in: int
    tokens_out: int
    new_summarized_through: int
    task_id: str = ""
    tenant_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class Tier3SkippedEvent:
    """Emitted when Tier 3 trigger is met but summarization was skipped."""

    reason: str  # 'retryable' | 'fatal' | 'cap_reached' | 'empty_slice'
    task_id: str = ""
    tenant_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class MemoryFlushFiredEvent:
    """Emitted when the pre-Tier-3 memory flush fires for this task.

    This event is one-shot per task: once ``memory_flush_fired_this_task`` is
    True, it will never fire again.  The event is NOT emitted on heartbeat
    turns (i.e., when ``_is_heartbeat_turn`` returns True).
    """

    fired_at_step: int
    task_id: str = ""
    tenant_id: str = ""
    agent_id: str = ""


CompactionEvent = HardFloorEvent | Tier1AppliedEvent | Tier15AppliedEvent | Tier3FiredEvent | Tier3SkippedEvent | MemoryFlushFiredEvent


# ---------------------------------------------------------------------------
# Pre-Tier-3 memory flush — prompt and helpers
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
    """Return True when no new messages have arrived since the last agent super-step.

    A heartbeat/recovery turn has no new ToolMessage or HumanMessage since
    the last agent super-step.  Detection is positional: compare the current
    message-list length against the watermark persisted at the end of the
    previous super-step.

    This is NOT the "last two messages are both AIMessage" heuristic, which
    misfires on rate-limit retry loops and pure-reasoning turns.
    """
    return len(raw_messages) <= last_super_step_message_count


def should_fire_pre_tier3_flush(
    state: dict[str, Any],
    agent_config: dict[str, Any],
    raw_messages: list[BaseMessage],
) -> bool:
    """Return True iff the pre-Tier-3 memory flush should fire on this call.

    All four conditions must hold:
    1. ``context_management.pre_tier3_memory_flush`` is True (default True).
    2. ``memory.enabled`` is True.
    3. ``memory_flush_fired_this_task`` is False (one-shot).
    4. NOT a heartbeat turn (new messages have arrived since last super-step).
    """
    ctx_mgmt: dict[str, Any] = agent_config.get("context_management") or {}
    if not ctx_mgmt.get("pre_tier3_memory_flush", True):
        return False

    memory_cfg: dict[str, Any] = agent_config.get("memory") or {}
    if memory_cfg.get("enabled") is not True:
        return False

    if state.get("memory_flush_fired_this_task", False):
        return False

    last_count: int = state.get("last_super_step_message_count", 0)
    if _is_heartbeat_turn(raw_messages, last_count):
        return False

    return True


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionPassResult:
    """Result of one ``compact_for_llm`` invocation.

    Attributes
    ----------
    messages:
        The compacted message view to pass to ``llm_with_tools.ainvoke``.
        Contains the summary marker as the first ``SystemMessage`` when Tier 3
        has fired at least once.
    state_updates:
        Dictionary of field updates to merge into graph state.  Always
        contains at least ``last_super_step_message_count``.
    events:
        Structured-log events to emit; the caller calls ``ev.log()`` or
        emits them via structlog.  The pipeline does NOT log directly so it
        remains testable without mocking the logger.
    tier3_skipped:
        ``True`` when Tier 3 trigger was met but summarization was not
        attempted (cap reached, fatal, retryable).
    """

    messages: list[BaseMessage]
    state_updates: dict[str, Any]
    events: list[CompactionEvent]
    tier3_skipped: bool = False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def compact_for_llm(
    raw_messages: list[BaseMessage],
    state: dict[str, Any],
    agent_config: dict[str, Any],
    model_context_window: int,
    task_context: dict[str, Any],
    summarizer: Any,
    *,
    estimate_tokens_fn: Callable[[list[BaseMessage]], int],
) -> CompactionPassResult:
    """Tiered compaction pipeline — runs before every ``llm_with_tools.ainvoke``.

    Args
    ----
    raw_messages:
        Full message list from graph state (``state["messages"]``).
    state:
        Read-only view of the current graph state (mapping).
    agent_config:
        Agent configuration dict including ``context_management`` sub-object.
    model_context_window:
        Token budget of the model resolved at graph-build time.
    task_context:
        Tenant/agent/task/checkpoint IDs + cost_ledger + callbacks.
    summarizer:
        Async callable matching the ``summarize_slice`` signature.  Injected
        for testability.
    estimate_tokens_fn:
        Callable ``(list[BaseMessage]) -> int`` — injected so tests can
        supply a deterministic estimate without a real tokenizer.

    Returns
    -------
    CompactionPassResult
        Never raises — all errors are captured in events or logged internally.
    """
    tenant_id: str = task_context.get("tenant_id", "")
    agent_id: str = task_context.get("agent_id", "")
    task_id: str = task_context.get("task_id", "")
    checkpoint_id: str | None = task_context.get("checkpoint_id")
    cost_ledger = task_context.get("cost_ledger")
    callbacks = task_context.get("callbacks") or []

    # Pull compaction-relevant state watermarks (with reducer-safe defaults)
    cleared_through: int = state.get("cleared_through_turn_index", 0)
    truncated_through: int = state.get("truncated_args_through_turn_index", 0)
    summarized_through: int = state.get("summarized_through_turn_index", 0)
    summary_marker: str | None = state.get("summary_marker") or ""
    tier3_firings_count: int = state.get("tier3_firings_count", 0)
    tier3_fatal: bool = bool(state.get("tier3_fatal_short_circuited", False))

    # Pull per-agent overrides from context_management sub-object
    ctx_mgmt: dict[str, Any] = (agent_config.get("context_management") or {})
    exclude_tools_extra: list[str] = ctx_mgmt.get("exclude_tools") or []
    exclude_tools_effective: frozenset[str] = PLATFORM_EXCLUDE_TOOLS | frozenset(
        exclude_tools_extra
    )

    # State updates dict — we'll accumulate updates here.
    # Watermark tracks the count of PERSISTED messages (state["messages"])
    # NOT the possibly system-prompt-prepended `raw_messages` passed in.  If
    # the caller injected transient SystemMessages before calling us, they
    # do not live in state["messages"] and using len(raw_messages) here would
    # cause the next super-step's conversation-log slice to start too far
    # ahead and silently drop newly-added user/tool entries.
    _persisted_messages = state.get("messages")
    if _persisted_messages is None:
        _persisted_messages = raw_messages
    state_updates: dict[str, Any] = {
        "last_super_step_message_count": len(_persisted_messages),
    }
    events: list[CompactionEvent] = []
    tier3_skipped = False

    # Resolve thresholds
    thresholds = resolve_thresholds(model_context_window)

    # Start with raw messages
    messages: list[BaseMessage] = raw_messages

    # ------------------------------------------------------------------
    # Prepend summary_marker as SystemMessage when non-empty.
    # Done BEFORE tokenization so the marker's tokens are counted.
    # ------------------------------------------------------------------
    if summary_marker:
        sys_summary = SystemMessage(
            content=summary_marker,
            additional_kwargs={"compaction": True},
        )
        # Build compacted view with the summary prefix
        # (only if the first message is not already this system message)
        if not (
            messages
            and isinstance(messages[0], SystemMessage)
            and messages[0].additional_kwargs.get("compaction") is True
        ):
            messages = [sys_summary] + list(messages)

    # ------------------------------------------------------------------
    # Step 1: estimate token count
    # ------------------------------------------------------------------
    est_tokens = estimate_tokens_fn(messages)

    # ------------------------------------------------------------------
    # Step 2: Tier 1 — tool-result clearing
    # ------------------------------------------------------------------
    if est_tokens > thresholds.tier1:
        clear_result = clear_tool_results(
            messages=messages,
            cleared_through_turn_index=cleared_through,
            keep=KEEP_TOOL_USES,
            exclude_tools_effective=exclude_tools_effective,
        )
        if clear_result.new_cleared_through_turn_index > cleared_through:
            # Watermark advanced — emit event and update state
            events.append(Tier1AppliedEvent(
                messages_cleared=clear_result.messages_cleared,
                est_tokens_saved=clear_result.est_tokens_saved,
                new_watermark=clear_result.new_cleared_through_turn_index,
                task_id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
            ))
            state_updates["cleared_through_turn_index"] = (
                clear_result.new_cleared_through_turn_index
            )
            cleared_through = clear_result.new_cleared_through_turn_index
        messages = clear_result.messages

        # Re-estimate after Tier 1
        est_tokens = estimate_tokens_fn(messages)

    # ------------------------------------------------------------------
    # Step 3: Tier 1.5 — tool-call argument truncation
    # ------------------------------------------------------------------
    if est_tokens > thresholds.tier1:
        trunc_result = truncate_tool_call_args(
            messages=messages,
            truncated_args_through_turn_index=truncated_through,
            keep=KEEP_TOOL_USES,
            truncatable_keys=TRUNCATABLE_TOOL_ARG_KEYS,
            cap_bytes=ARG_TRUNCATION_CAP_BYTES,
        )
        if trunc_result.new_truncated_args_through_turn_index > truncated_through:
            events.append(Tier15AppliedEvent(
                args_truncated=trunc_result.args_truncated,
                bytes_saved=trunc_result.bytes_saved,
                new_watermark=trunc_result.new_truncated_args_through_turn_index,
                task_id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
            ))
            state_updates["truncated_args_through_turn_index"] = (
                trunc_result.new_truncated_args_through_turn_index
            )
            truncated_through = trunc_result.new_truncated_args_through_turn_index
        messages = trunc_result.messages

        # Re-estimate after Tier 1.5
        est_tokens = estimate_tokens_fn(messages)

    # ------------------------------------------------------------------
    # Step 4: Tier 3 — LLM summarization (last resort)
    # ------------------------------------------------------------------
    if est_tokens > thresholds.tier3:
        # Gate 1: fatal short-circuit (misconfigured summarizer)
        if tier3_fatal:
            # Silent skip — no event, no cost
            tier3_skipped = True

        # Gate 2: firings cap
        elif tier3_firings_count >= TIER_3_MAX_FIRINGS_PER_TASK:
            events.append(Tier3SkippedEvent(
                reason="cap_reached",
                task_id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
            ))
            tier3_skipped = True

        else:
            # Determine the slice to summarize.
            #
            # All Tier 3 indexing — summarized_through, protect_from_index,
            # new_summarized_through — must use the RAW (compaction-SystemMessage-
            # stripped) view. ``state.summarized_through_turn_index`` is
            # indexed into raw messages, and the tail-rebuild below strips
            # compaction SystemMessages before slicing, so mixing indexing
            # spaces stranded a ToolMessage off-by-one on subsequent Tier 3
            # firings (the prior firing prepends a SystemMessage that shifts
            # ``messages`` indices by +1). See test_compaction_tier3_
            # second_firing_boundary.py for the regression.
            #
            # protect_from_index must also land on an AIMessage or
            # HumanMessage, never a bare ToolMessage — otherwise the tail
            # begins with an orphan toolResult whose tool_use was summarized
            # away (first-firing variant of the bug).
            raw_view: list[BaseMessage] = [
                m for m in messages
                if not (
                    isinstance(m, SystemMessage)
                    and m.additional_kwargs.get("compaction") is True
                )
            ]
            tool_positions = [
                i for i, m in enumerate(raw_view) if isinstance(m, ToolMessage)
            ]
            if len(tool_positions) > KEEP_TOOL_USES:
                protect_from_index = tool_positions[-KEEP_TOOL_USES]
                if isinstance(raw_view[protect_from_index], ToolMessage):
                    for j in range(protect_from_index - 1, -1, -1):
                        candidate = raw_view[j]
                        if isinstance(candidate, AIMessage) and candidate.tool_calls:
                            protect_from_index = j
                            break
            else:
                protect_from_index = len(raw_view)

            # Task 9: pre-Tier-3 memory flush hook.
            # If conditions are met, insert the flush SystemMessage at the END
            # of the compacted messages view (in-memory only — NOT persisted to
            # graph state) and return early, skipping Tier 3 this call.
            if should_fire_pre_tier3_flush(state, agent_config, raw_messages):
                flush_message = SystemMessage(
                    content=_PRE_TIER3_FLUSH_PROMPT,
                    additional_kwargs={
                        "compaction": True,
                        "compaction_event": "pre_tier3_memory_flush",
                    },
                )
                state_updates["memory_flush_fired_this_task"] = True
                events.append(MemoryFlushFiredEvent(
                    fired_at_step=len(raw_messages),
                    task_id=task_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                ))
                # Recompute tokens with the flush message appended — it adds
                # non-trivial text and can push us over the hard floor.  If
                # the pre-flush `est_tokens` was already close to the limit,
                # skipping this check would have the next LLM call fail with
                # a provider context-limit error instead of taking the
                # explicit dead-letter path.
                flush_view = [*messages, flush_message]
                est_tokens_with_flush = estimate_tokens_fn(flush_view)
                if est_tokens_with_flush > model_context_window:
                    events.append(HardFloorEvent(
                        est_tokens=est_tokens_with_flush,
                        model_context_window=model_context_window,
                        task_id=task_id,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                    ))
                return CompactionPassResult(
                    messages=flush_view,
                    state_updates=state_updates,
                    events=events,
                    tier3_skipped=False,
                )

            slice_messages = raw_view[summarized_through:protect_from_index]
            new_summarized_through = protect_from_index

            # Get the summarizer model ID
            summarizer_model_id: str = (
                ctx_mgmt.get("summarizer_model")
                or get_platform_default_summarizer_model()
            )

            # Call summarize_slice (or the injected mock)
            summarize_result = await summarizer(
                slice_messages=slice_messages,
                summarizer_model_id=summarizer_model_id,
                task_id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                checkpoint_id=checkpoint_id,
                cost_ledger=cost_ledger,
                callbacks=callbacks,
                summarized_through_turn_index_after=new_summarized_through,
            )

            if summarize_result.skipped:
                # Handle fatal vs retryable
                if summarize_result.skipped_reason == "fatal":
                    state_updates["tier3_fatal_short_circuited"] = True
                events.append(Tier3SkippedEvent(
                    reason=summarize_result.skipped_reason or "unknown",
                    task_id=task_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                ))
                tier3_skipped = True

            else:
                # Success: advance watermark, update summary_marker, rebuild messages
                summary_text = summarize_result.summary_text or ""

                # Append to the existing marker (strict-append)
                if summary_marker:
                    # Build the new appended string
                    new_marker = summary_marker + summary_text
                else:
                    new_marker = summary_text

                state_updates["summarized_through_turn_index"] = new_summarized_through
                state_updates["summary_marker"] = new_marker
                state_updates["tier3_firings_count"] = tier3_firings_count + 1

                # Rebuild message list: [SystemMessage(summary_marker), *tail]
                # Slice over raw_view so the index matches the space
                # protect_from_index / new_summarized_through were computed in.
                tail_messages = raw_view[new_summarized_through:]

                sys_summary = SystemMessage(
                    content=new_marker,
                    additional_kwargs={"compaction": True},
                )
                messages = [sys_summary] + tail_messages

                events.append(Tier3FiredEvent(
                    summarizer_model_id=summarize_result.summarizer_model_id,
                    tokens_in=summarize_result.tokens_in,
                    tokens_out=summarize_result.tokens_out,
                    new_summarized_through=new_summarized_through,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                ))

                # Re-estimate after Tier 3
                est_tokens = estimate_tokens_fn(messages)
                # Update summary_marker for the hard-floor check below
                summary_marker = new_marker
                summarized_through = new_summarized_through

    # ------------------------------------------------------------------
    # Step 5: Hard floor check — emit HardFloorEvent if still over limit
    # ------------------------------------------------------------------
    if est_tokens > model_context_window:
        events.append(HardFloorEvent(
            est_tokens=est_tokens,
            model_context_window=model_context_window,
            task_id=task_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
        ))

    return CompactionPassResult(
        messages=messages,
        state_updates=state_updates,
        events=events,
        tier3_skipped=tier3_skipped,
    )
