"""Reader adapter for the ``task_history_get`` tool's ``tool_calls`` field.

The LangGraph checkpointer is the authoritative source for per-super-step
agent state, including every ``AIMessage.tool_calls`` and matching
``ToolMessage`` result. This module pulls that data out of the latest
checkpoint for a given task and returns a bounded, structured view.

Why a dedicated file
--------------------
``task_history_get`` sits at the boundary between LangGraph's internal
message format and a stable tool-return shape visible to the LLM. If
LangGraph changes how ``AIMessage.tool_calls`` or ``ToolMessage.content``
are spelled on a minor upgrade, we want exactly **one** file to touch —
not a field read scattered across the codebase. This adapter is the
single point of coupling.

Failure policy
--------------
Any exception while reading or walking the checkpoint (missing tuple,
deserialization failure, unexpected message shape) is caught, logged as
``memory.task_history.tool_calls_read_failed``, and surfaced as an empty
``tool_calls`` list. That way a LangGraph version drift can only
**degrade** the tool's output, never crash the calling agent.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _preview(value: Any, max_bytes: int) -> str | None:
    """Serialize and truncate a tool-call arg or result for LLM consumption."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            import json as _json

            text = _json.dumps(value, default=str, ensure_ascii=False)
        except Exception:
            text = str(value)
    if len(text) <= max_bytes:
        return text
    return text[: max_bytes - 1] + "…"


def _extract_tool_calls_from_ai_message(msg: Any) -> list[dict[str, Any]]:
    """Pull ``{id, name, args}`` triples from an AIMessage's ``tool_calls``.

    Handles both the modern LangChain shape (``tool_calls`` as a list of
    dicts with keys ``id``, ``name``, ``args``) and the Anthropic-style
    content-block shape (``tool_use`` blocks). Ignores anything unrecognized.
    """
    results: list[dict[str, Any]] = []

    # Modern shape: AIMessage.tool_calls is a list[dict].
    raw = getattr(msg, "tool_calls", None)
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            call_id = entry.get("id")
            name = entry.get("name")
            args = entry.get("args")
            if not name:
                continue
            results.append({"id": call_id, "name": name, "args": args})

    # Content-block shape: message.content is a list with items where
    # ``type == "tool_use"``. Used by Anthropic provider when LangChain
    # passes blocks through untouched.
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            call_id = block.get("id")
            name = block.get("name")
            args = block.get("input")
            if not name:
                continue
            # De-dupe: if we already captured this id via tool_calls, skip.
            if any(r.get("id") == call_id for r in results if call_id):
                continue
            results.append({"id": call_id, "name": name, "args": args})

    return results


def _tool_message_content(msg: Any) -> Any:
    """Return the raw content of a ToolMessage (string or list-of-blocks)."""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    # Some providers wrap tool results as a list of {type: "text", text: ...}
    # content blocks. Flatten to the concatenated text.
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    return content


async def read_tool_calls(
    checkpointer: Any,
    task_id: str,
    *,
    cap: int,
    preview_bytes: int,
) -> list[dict[str, Any]]:
    """Read the tool-call history for a past task out of the checkpointer.

    Parameters
    ----------
    checkpointer:
        A LangGraph checkpointer instance. The worker's
        :class:`PostgresDurableCheckpointer` satisfies this.
    task_id:
        The task id of interest. Used as ``thread_id`` in the config.
    cap:
        Maximum number of tool-call entries to return (oldest-first).
    preview_bytes:
        Byte cap per ``args_preview`` and ``result_preview`` field.

    Returns
    -------
    A list of ``{"name", "args_preview", "result_preview"}`` dicts.
    Empty list when there are no tool calls, when the checkpoint is
    missing, or when the read fails for any reason.
    """
    if checkpointer is None or not task_id:
        return []

    try:
        config: dict[str, Any] = {"configurable": {"thread_id": task_id}}
        tup = await checkpointer.aget_tuple(config)
        if tup is None:
            return []
        checkpoint = getattr(tup, "checkpoint", None) or {}
        if not isinstance(checkpoint, dict):
            return []
        values = checkpoint.get("channel_values") or {}
        messages = values.get("messages") if isinstance(values, dict) else None
        if not isinstance(messages, list):
            return []

        # Pass 1: collect tool calls in order; remember which id belongs to
        # which tool name so we can pair ToolMessage results in pass 2.
        # Using a list to preserve the order the agent issued them in —
        # ``tool_call_id`` uniqueness lets a dict fall back if we collide.
        ordered: list[dict[str, Any]] = []
        id_to_index: dict[str, int] = {}
        for msg in messages:
            calls = _extract_tool_calls_from_ai_message(msg)
            for call in calls:
                entry = {
                    "name": call["name"],
                    "args_preview": _preview(call.get("args"), preview_bytes),
                    "result_preview": None,
                }
                ordered.append(entry)
                call_id = call.get("id")
                if call_id:
                    id_to_index[str(call_id)] = len(ordered) - 1

        # Pass 2: attach results from matching ToolMessages. Orphan ToolMessages
        # (no matching call id) are silently ignored — shouldn't happen in a
        # sane graph but we don't want to crash on it.
        for msg in messages:
            call_id = getattr(msg, "tool_call_id", None)
            if not call_id:
                continue
            idx = id_to_index.get(str(call_id))
            if idx is None:
                continue
            ordered[idx]["result_preview"] = _preview(
                _tool_message_content(msg), preview_bytes
            )

        return ordered[:cap]
    except Exception:
        logger.warning(
            "memory.task_history.tool_calls_read_failed task_id=%s",
            task_id,
            exc_info=True,
        )
        return []
