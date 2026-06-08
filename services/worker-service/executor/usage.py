"""Provider-neutral LLM usage accumulation helpers (Agent Modes, §A11-E1).

Shared by the Supervisor cost path (``executor/supervisor/cost.py``) and the
fan-out sub-agent helper (``executor/subagents/fanout.py``). Lives at
``executor.usage`` — a leaf module depending only on ``langchain_core`` — so the
lower-level ``subagents`` primitive and the higher-level ``supervisor`` package
can both use it without a circular dependency.

These produce/merge a flat ``usage_metadata``-shaped dict (the standard
LangChain numeric token fields) so the existing provider-aware cost extractor
(``executor.graph._extract_token_usage`` → per-provider ``PromptCacheStrategy``)
consumes the accumulated total unchanged — no provider branches added here
(§LLM Provider Support).
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage


def usage_from_message(msg: Any) -> dict[str, int]:
    """Extract a flat ``usage_metadata``-shaped dict from one AIMessage.

    Returns ``{}`` when the message carries no ``usage_metadata`` (some
    providers / streaming paths omit it — then no spend is attributed for that
    message). Cache counters nested under LangChain's ``input_token_details``
    are flattened to the top-level ``cache_*_input_tokens`` keys; ``input_tokens``
    is left as the provider reported it (the per-provider extractor owns any
    inclusive-vs-exclusive normalization — see ``prompt_cache/anthropic.py``).
    """
    um = getattr(msg, "usage_metadata", None)
    if not isinstance(um, dict) or not um:
        return {}
    out: dict[str, int] = {}
    inp = um.get("input_tokens")
    if inp:
        out["input_tokens"] = int(inp)
    outp = um.get("output_tokens")
    if outp:
        out["output_tokens"] = int(outp)

    details = um.get("input_token_details") or {}
    cache_creation = (
        details.get("cache_creation")
        or um.get("cache_creation_input_tokens")
        or 0
    )
    cache_read = (
        details.get("cache_read") or um.get("cache_read_input_tokens") or 0
    )
    if cache_creation:
        out["cache_creation_input_tokens"] = int(cache_creation)
    if cache_read:
        out["cache_read_input_tokens"] = int(cache_read)
    return out


def merge_usage(
    a: dict[str, int] | None, b: dict[str, int] | None
) -> dict[str, int]:
    """Additive per-field merge of two usage dicts.

    Named (not ``dict.update`` / a lambda) and additive because LangGraph 1.0.5
    introspects a channel reducer's signature (rejects un-introspectable
    builtins) AND because parallel ``Send`` branches each write a usage delta
    into the SAME channel in one super-step — last-write-wins would silently
    drop all-but-one branch's spend (the exact §A11-E1 gap). Never mutates its
    inputs.
    """
    merged: dict[str, int] = dict(a or {})
    for k, v in (b or {}).items():
        merged[k] = merged.get(k, 0) + int(v or 0)
    return merged


def usage_from_messages(messages: list[BaseMessage] | None) -> dict[str, int]:
    """Sum :func:`usage_from_message` over a list of messages."""
    acc: dict[str, int] = {}
    for msg in messages or []:
        acc = merge_usage(acc, usage_from_message(msg))
    return acc
