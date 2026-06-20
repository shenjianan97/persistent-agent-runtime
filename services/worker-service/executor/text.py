"""Read-boundary text helpers for LangChain message content.

LangChain's ``BaseMessage.content`` is a union type (``str | list[block]``).
Block shapes are provider-specific: Anthropic, OpenAI, Gemini, and Bedrock each
emit different wrappers. These helpers extract plain text at **read** boundaries
only — never at persist-time (persisting normalised content breaks Anthropic
prompt caching and OpenAI reasoning continuation; see CLAUDE.md §LLM Provider
Support).
"""

from __future__ import annotations

from typing import Any


def flatten_text(content: Any) -> str:
    """Flatten LangChain message content (``str | list[block]``) to plain text.

    Provider-shaped block lists carry text under ``{"type": "text", "text": …}``
    or bare ``{"text": …}``; every other value stringifies. Read-boundary only —
    do not call at persist-time.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)
