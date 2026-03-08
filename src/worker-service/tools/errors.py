"""Tool-specific error types for Phase 1 MCP handlers."""

from __future__ import annotations


class ToolExecutionError(RuntimeError):
    """Base error raised by Phase 1 tool handlers."""


class ToolInputError(ToolExecutionError):
    """Raised when a request is valid JSON but violates tool-specific rules."""


class ToolTransportError(ToolExecutionError):
    """Raised when a transient backend or network dependency fails."""
