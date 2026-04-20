"""Tier 0 ingestion-offload helpers (Phase 2 Track 7 Follow-up, Task 4).

Two entry points:

- :func:`offload_tool_message` — inspects a ``ToolMessage`` returned by the
  tool node and, if its content exceeds ``OFFLOAD_THRESHOLD_BYTES``, writes
  the raw content to the artifact store and returns a new ``ToolMessage``
  whose ``content`` is a bounded placeholder containing the URI + preview.
- :func:`offload_ai_message_args` — inspects an ``AIMessage`` that carries
  ``tool_calls``, walks each call's ``args`` dict, and offloads any string
  value whose key is in ``TRUNCATABLE_ARG_KEYS`` and whose UTF-8 length
  exceeds ``OFFLOAD_THRESHOLD_BYTES``. Returns a new ``AIMessage`` with
  reference-replaced args.

Both helpers honour the per-candidate fail-closed contract: if ``store.put``
raises for a given item, that item stays inline (the raise happened before
replacement, so no state was mutated), a WARN is logged, and other candidates
continue normally. If every candidate in a single call raises, a single
``compaction.offload_all_failed`` WARN is emitted.

Neither helper mutates its input message or its ``args`` dict; they construct
new instances via ``model_copy`` / fresh dicts.

TODO(Task 5): both helpers currently leave a hook for the
``offload_emitted`` conversation-log event. Task 5 owns emission — do not
emit yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

import structlog
from langchain_core.messages import AIMessage, ToolMessage

from executor.compaction.defaults import (
    OFFLOAD_THRESHOLD_BYTES,
    TRUNCATABLE_ARG_KEYS,
)
from executor.compaction.tool_result_store import ToolResultArtifactStore


_logger = structlog.get_logger(__name__)

# Preview rules: first ~5 lines OR first ~500 bytes, whichever shorter.
PREVIEW_MAX_LINES: int = 5
PREVIEW_MAX_BYTES: int = 500


@dataclass(frozen=True)
class OffloadOutcome:
    """Result of an ingestion-offload pass over one message.

    Attributes:
        message: The (possibly rewritten) message. Same instance as the input
            when no work was done.
        events: One entry per offload event (success or per-candidate
            failure). Used by Task 5 to emit ``offload_emitted`` /
            ``offload_failed`` into the conversation log. Task 4 wiring only
            consumes these for structured log emission.
    """

    message: AIMessage | ToolMessage
    events: tuple["OffloadEvent", ...] = ()


@dataclass(frozen=True)
class OffloadEvent:
    """One ingestion-offload event (success or per-candidate failure).

    Attributes:
        kind: ``"success"`` or ``"failed"``.
        variant: ``"result"`` for tool-result offload; ``"arg"`` for tool-call
            arg offload.
        tool_call_id: The tool-call id associated with the event.
        tool_name: Tool name (result variant) or the tool the args belong to.
        arg_key: Only set for ``variant == "arg"``.
        size_bytes: UTF-8 byte length of the original content (pre-offload).
        uri: URI returned by the store on success; ``None`` on failure.
        error_type: ``type(exc).__name__`` on failure; ``None`` on success.
        error_message: Truncated to ~200 chars; ``None`` on success.
    """

    kind: str
    variant: str
    tool_call_id: str
    tool_name: str | None
    arg_key: str | None
    size_bytes: int
    uri: str | None
    error_type: str | None
    error_message: str | None


def _utf8_preview(content: str) -> str:
    """Build a short, UTF-8-safe preview per the spec's preview rule.

    First ~5 lines OR first ~500 bytes, whichever is shorter.
    """
    lines = content.splitlines()
    by_lines = "\n".join(lines[:PREVIEW_MAX_LINES])
    by_bytes_data = content.encode("utf-8")[:PREVIEW_MAX_BYTES]
    # Decode with "ignore" to drop an incomplete multi-byte codepoint at the
    # boundary rather than emit U+FFFD replacements.
    by_bytes = by_bytes_data.decode("utf-8", errors="ignore")
    return by_lines if len(by_lines.encode("utf-8")) <= len(by_bytes.encode("utf-8")) else by_bytes


def _result_placeholder(*, size_bytes: int, uri: str, preview: str) -> str:
    return f"[tool result {size_bytes} bytes @ {uri} preview: {preview}]"


def _arg_placeholder(
    *, key: str, size_bytes: int, uri: str, preview: str
) -> str:
    return (
        f"[tool arg '{key}' {size_bytes} bytes @ {uri} preview: {preview}]"
    )


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


async def offload_tool_message(
    msg: ToolMessage,
    *,
    store: ToolResultArtifactStore,
    tenant_id: str,
    task_id: str,
    threshold_bytes: int = OFFLOAD_THRESHOLD_BYTES,
    log_context: dict[str, Any] | None = None,
) -> OffloadOutcome:
    """Maybe offload ``msg.content`` to the store and return a placeholder.

    Below ``threshold_bytes`` (UTF-8 byte length): no-op, returns the input
    verbatim. At / above threshold: uploads the raw content, constructs a
    new ToolMessage whose content is the placeholder string. Non-string
    content is passed through unchanged (defensive; current tools always
    return strings after the LangGraph wrapper).

    Fail-closed: any exception raised by ``store.put`` is caught, logged at
    WARN, and the original message is returned unchanged.
    """
    content = msg.content
    if not isinstance(content, str):
        return OffloadOutcome(message=msg)
    size_bytes = _byte_len(content)
    if size_bytes <= threshold_bytes:
        return OffloadOutcome(message=msg)

    tool_call_id: str = getattr(msg, "tool_call_id", "") or ""
    tool_name: str = getattr(msg, "name", "") or ""
    log_ctx = dict(log_context or {})

    try:
        uri = await store.put(
            tenant_id=tenant_id,
            task_id=task_id,
            tool_call_id=tool_call_id,
            content=content,
        )
    except Exception as e:  # noqa: BLE001 — fail-closed intentionally broad
        _logger.warning(
            "compaction.offload_failed",
            variant="result",
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            size_bytes=size_bytes,
            error_type=type(e).__name__,
            error_message=str(e)[:200],
            **log_ctx,
        )
        event = OffloadEvent(
            kind="failed",
            variant="result",
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arg_key=None,
            size_bytes=size_bytes,
            uri=None,
            error_type=type(e).__name__,
            error_message=str(e)[:200],
        )
        return OffloadOutcome(message=msg, events=(event,))

    preview = _utf8_preview(content)
    placeholder = _result_placeholder(
        size_bytes=size_bytes, uri=uri, preview=preview
    )

    # Construct a new ToolMessage (model_copy preserves id/status/etc.)
    new_msg = msg.model_copy(update={"content": placeholder})
    # TODO(Task 5): emit `offload_emitted` conversation-log event here
    # using (tool_call_id, tool_name, variant="result", size_bytes, uri).
    event = OffloadEvent(
        kind="success",
        variant="result",
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        arg_key=None,
        size_bytes=size_bytes,
        uri=uri,
        error_type=None,
        error_message=None,
    )
    return OffloadOutcome(message=new_msg, events=(event,))


async def offload_ai_message_args(
    msg: AIMessage,
    *,
    store: ToolResultArtifactStore,
    tenant_id: str,
    task_id: str,
    threshold_bytes: int = OFFLOAD_THRESHOLD_BYTES,
    truncatable_keys: frozenset[str] = TRUNCATABLE_ARG_KEYS,
    log_context: dict[str, Any] | None = None,
) -> OffloadOutcome:
    """Walk ``msg.tool_calls`` and offload truncatable, oversized string args.

    For each call in ``msg.tool_calls``, for each key in ``args`` that (1) is
    in ``truncatable_keys``, (2) has a string value, and (3) whose UTF-8
    length exceeds ``threshold_bytes``, uploads the raw value and replaces
    it in the (freshly-constructed) args dict with a placeholder.

    Fail-closed per candidate: if ``store.put`` raises for a given arg, that
    arg stays inline, a WARN is logged, and the next candidate proceeds.
    """
    tool_calls = list(msg.tool_calls or [])
    if not tool_calls:
        return OffloadOutcome(message=msg)

    log_ctx = dict(log_context or {})

    events: list[OffloadEvent] = []
    new_tool_calls: list[dict[str, Any]] = []
    any_touched = False
    total_candidates = 0
    total_failed = 0

    for call in tool_calls:
        call_dict: dict[str, Any] = dict(call) if isinstance(call, dict) else dict(call)
        call_id: str = call_dict.get("id") or ""
        call_name: str = call_dict.get("name") or ""
        args: dict[str, Any] = dict(call_dict.get("args") or {})
        touched = False

        for key in list(args.keys()):
            if key not in truncatable_keys:
                continue
            value = args[key]
            if not isinstance(value, str):
                continue
            size_bytes = _byte_len(value)
            if size_bytes <= threshold_bytes:
                continue

            total_candidates += 1
            try:
                uri = await store.put(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    tool_call_id=call_id,
                    content=value,
                    arg_key=key,
                )
            except Exception as e:  # noqa: BLE001 — fail-closed intentionally broad
                total_failed += 1
                _logger.warning(
                    "compaction.offload_failed",
                    variant="arg",
                    tool_call_id=call_id,
                    tool_name=call_name,
                    arg_key=key,
                    size_bytes=size_bytes,
                    error_type=type(e).__name__,
                    error_message=str(e)[:200],
                    **log_ctx,
                )
                events.append(
                    OffloadEvent(
                        kind="failed",
                        variant="arg",
                        tool_call_id=call_id,
                        tool_name=call_name,
                        arg_key=key,
                        size_bytes=size_bytes,
                        uri=None,
                        error_type=type(e).__name__,
                        error_message=str(e)[:200],
                    )
                )
                continue

            preview = _utf8_preview(value)
            args[key] = _arg_placeholder(
                key=key, size_bytes=size_bytes, uri=uri, preview=preview
            )
            touched = True
            # TODO(Task 5): emit `offload_emitted` conversation-log event here
            # using (tool_call_id=call_id, tool_name=call_name, variant="arg",
            # arg_key=key, size_bytes, uri).
            events.append(
                OffloadEvent(
                    kind="success",
                    variant="arg",
                    tool_call_id=call_id,
                    tool_name=call_name,
                    arg_key=key,
                    size_bytes=size_bytes,
                    uri=uri,
                    error_type=None,
                    error_message=None,
                )
            )

        if touched:
            any_touched = True
            new_tool_calls.append({**call_dict, "args": args})
        else:
            new_tool_calls.append(call_dict)

    # All-failed in one pass → one-shot WARN. "One pass" = one call to this
    # helper with ≥1 candidate and zero successes.
    if total_candidates > 0 and total_failed == total_candidates:
        _logger.warning(
            "compaction.offload_all_failed",
            variant="arg",
            failed_count=total_failed,
            **log_ctx,
        )

    if not any_touched:
        return OffloadOutcome(message=msg, events=tuple(events))

    new_msg = msg.model_copy(update={"tool_calls": new_tool_calls})
    return OffloadOutcome(message=new_msg, events=tuple(events))


async def offload_tool_messages_batch(
    messages: Sequence[ToolMessage],
    *,
    store: ToolResultArtifactStore,
    tenant_id: str,
    task_id: str,
    threshold_bytes: int = OFFLOAD_THRESHOLD_BYTES,
    log_context: dict[str, Any] | None = None,
) -> tuple[list[ToolMessage], tuple[OffloadEvent, ...]]:
    """Apply :func:`offload_tool_message` to each message; aggregate events.

    Emits a single ``compaction.offload_all_failed`` WARN when every oversized
    candidate in the batch failed. Callers that produce a single ToolMessage
    at a time can use :func:`offload_tool_message` directly — this helper is
    here for the ToolNode wiring path that receives a list of ToolMessages
    per super-step.
    """
    log_ctx = dict(log_context or {})
    out: list[ToolMessage] = []
    agg_events: list[OffloadEvent] = []
    total_candidates = 0
    total_failed = 0
    for m in messages:
        if isinstance(m.content, str) and _byte_len(m.content) > threshold_bytes:
            total_candidates += 1
        outcome = await offload_tool_message(
            m,
            store=store,
            tenant_id=tenant_id,
            task_id=task_id,
            threshold_bytes=threshold_bytes,
            log_context=log_ctx,
        )
        if any(ev.kind == "failed" for ev in outcome.events):
            total_failed += 1
        out.append(outcome.message)  # type: ignore[arg-type]
        agg_events.extend(outcome.events)
    if total_candidates > 0 and total_failed == total_candidates:
        _logger.warning(
            "compaction.offload_all_failed",
            variant="result",
            failed_count=total_failed,
            **log_ctx,
        )
    return out, tuple(agg_events)


__all__ = [
    "OffloadEvent",
    "OffloadOutcome",
    "offload_ai_message_args",
    "offload_tool_message",
    "offload_tool_messages_batch",
]
