"""Tool-specific error types for Phase 1 MCP handlers."""

from __future__ import annotations


class ToolExecutionError(RuntimeError):
    """Base error raised by Phase 1 tool handlers."""


class ToolInputError(ToolExecutionError):
    """Raised when a request is valid JSON but violates tool-specific rules."""


class ToolTransportError(ToolExecutionError):
    """Raised when a tool's network fetch fails (DNS, timeout, HTTP 5xx, …).

    These are failures of an *agent-chosen target* (a URL, a search query),
    not platform infrastructure: the worker's ToolNode error handler
    (``executor.graph._handle_tool_error``) surfaces them to the LLM as a
    correctable error ToolMessage so the agent can adapt — it does NOT
    trigger a task-level retry. Raisers should make the message actionable
    (include the target and a hint), and may perform a single bounded
    in-tool retry for transient flavors before raising.
    """
