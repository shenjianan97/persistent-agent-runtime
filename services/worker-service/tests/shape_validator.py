"""Provider-agnostic LLM conversation shape validator.

Encodes the tool_use/tool_result pairing rules that Anthropic, Bedrock Converse,
OpenAI, and Google/Gemini all enforce on conversations sent to their chat
completion APIs. Although the wire formats differ, the structural invariants
are the same when expressed over LangChain's canonical message types:

    AIMessage   — assistant turn; may carry ``tool_calls`` with unique IDs
    ToolMessage — tool-result turn; carries ``tool_call_id`` referencing an
                  earlier AIMessage tool_call
    HumanMessage — user turn
    SystemMessage — extracted to provider-specific ``system`` parameter;
                    not part of the ordered messages array

Rules enforced (all provider-agnostic):

1.  **No orphan ToolMessage.** Every ToolMessage's ``tool_call_id`` must
    reference a ``tool_call`` on some earlier AIMessage in the same list.
    The first production bug this validator caught: Tier 3 summarization
    stranded a ToolMessage whose paired AIMessage had been replaced by the
    summary SystemMessage. Bedrock rejected it with::

        Expected toolResult blocks at messages.0.content for the
        following Ids: tooluse_7SUeRPIQyQHnVwJx5kKzxP

2.  **No leading ToolMessage.** After stripping SystemMessages, the first
    message must be an AIMessage or HumanMessage. Starting with a ToolMessage
    means a tool_result has no preceding tool_call — all providers reject.

3.  **Every AIMessage tool_call has a matching ToolMessage.** When sending
    a conversation to the LLM, any assistant turn that requested tools must
    have tool_result messages covering every ``tool_call_id``. Pending
    tool_calls would fail provider validation.

Usage::

    from tests.shape_validator import LLMConversationShapeValidator, ShapeViolation

    validator = LLMConversationShapeValidator()
    validator.validate(messages)  # raises ShapeViolation on failure

Keep the rule set conservative — only invariants all major providers agree
on. Provider-specific quirks (e.g. Bedrock's alternation requirement) belong
in a dedicated subclass, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


class ShapeViolation(AssertionError):
    """Raised when a message list violates provider shape rules.

    Subclasses ``AssertionError`` so it surfaces cleanly in pytest output
    without special handling.
    """


@dataclass
class LLMConversationShapeValidator:
    """Validate LangChain message lists against provider-agnostic shape rules.

    The validator is stateless — safe to reuse across tests.
    """

    def validate(self, messages: list[BaseMessage]) -> None:
        """Raise ``ShapeViolation`` if ``messages`` violates any shape rule.

        Accepts the raw LangChain message list including any leading
        SystemMessages (which are stripped internally before shape checks).
        """
        non_system = [m for m in messages if not isinstance(m, SystemMessage)]

        if not non_system:
            return

        self._check_no_leading_tool_message(non_system)
        self._check_no_orphan_tool_results(non_system)
        self._check_no_pending_tool_calls(non_system)

    @staticmethod
    def _check_no_leading_tool_message(messages: list[BaseMessage]) -> None:
        first = messages[0]
        if isinstance(first, ToolMessage):
            raise ShapeViolation(
                "Rule violated: conversation begins with a ToolMessage "
                f"(tool_call_id={first.tool_call_id!r}). After stripping "
                "SystemMessages, the first message must be AIMessage or "
                "HumanMessage — a tool_result with no preceding tool_call "
                "is rejected by every major provider."
            )

    @staticmethod
    def _check_no_orphan_tool_results(messages: list[BaseMessage]) -> None:
        seen_tool_call_ids: set[str] = set()
        for idx, msg in enumerate(messages):
            if isinstance(msg, AIMessage):
                for call in (msg.tool_calls or []):
                    call_id = _tool_call_id(call)
                    if call_id:
                        seen_tool_call_ids.add(call_id)
            elif isinstance(msg, ToolMessage):
                if msg.tool_call_id not in seen_tool_call_ids:
                    raise ShapeViolation(
                        f"Rule violated: ToolMessage at index {idx} has "
                        f"tool_call_id={msg.tool_call_id!r} but no prior "
                        "AIMessage declared a matching tool_call. "
                        "Orphan tool_results are rejected by all providers."
                    )

    @staticmethod
    def _check_no_pending_tool_calls(messages: list[BaseMessage]) -> None:
        # Collect all tool_call_ids answered by some ToolMessage anywhere
        # in the list.
        answered: set[str] = {
            m.tool_call_id
            for m in messages
            if isinstance(m, ToolMessage)
        }
        for idx, msg in enumerate(messages):
            if not isinstance(msg, AIMessage):
                continue
            for call in (msg.tool_calls or []):
                call_id = _tool_call_id(call)
                if call_id and call_id not in answered:
                    tool_name = _tool_call_name(call)
                    raise ShapeViolation(
                        f"Rule violated: AIMessage at index {idx} has a "
                        f"pending tool_call (id={call_id!r}, name={tool_name!r}) "
                        "with no matching ToolMessage in the list. When sent "
                        "to the LLM, the provider will reject the unmatched id."
                    )


def _tool_call_id(call: Any) -> str | None:
    if isinstance(call, dict):
        return call.get("id")
    return getattr(call, "id", None)


def _tool_call_name(call: Any) -> str | None:
    if isinstance(call, dict):
        return call.get("name")
    return getattr(call, "name", None)


# Convenience re-exports so test files can do:
#   from tests.shape_validator import validator, ShapeViolation
#   validator.validate(result.messages)
validator = LLMConversationShapeValidator()


def assert_valid_shape(messages: list[BaseMessage]) -> None:
    """Shortcut — raises ShapeViolation on first violation."""
    validator.validate(messages)


# Re-export of imports used by consumers
__all__ = [
    "LLMConversationShapeValidator",
    "ShapeViolation",
    "assert_valid_shape",
    "validator",
    # Re-exported for convenience
    "AIMessage",
    "HumanMessage",
    "ToolMessage",
    "SystemMessage",
]
