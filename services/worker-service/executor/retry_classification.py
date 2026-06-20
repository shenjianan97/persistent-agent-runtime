"""Shared transient-vs-permanent error classification.

ONE classifier decides whether an exception is worth retrying, used by two
consumers with the same semantics:

* the task-level dead-letter decision (``GraphExecutor._is_retryable_error``
  delegates here) — a retryable error re-queues the task, which resumes from
  its checkpoint;
* the sub-agent per-turn ``RetryPolicy`` (``executor/subagents/fanout.py``) —
  ``run_subagent`` converts exceptions into failure markers instead of
  raising, which cuts fan-out branches off from the task-level retry path, so
  the same classification must gate an in-place retry of the model call.

Extracted from ``GraphExecutor`` (S11 follow-up review): a second hand-rolled
predicate in fanout had already drifted from this one — keeping a single
definition is the point.
"""

from __future__ import annotations

import re

from executor.mcp_session import McpToolCallError

# Status codes that are safe to retry (transient server / rate-limit errors).
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}


def walk_exception_chain(e: Exception):
    """Yield each exception in the ``__cause__``/``__context__`` chain (including ``e``)."""
    current = e
    for _ in range(5):
        if current is None:
            break
        yield current
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)


def extract_status_code(e: Exception) -> int | None:
    """Walk the exception chain to find an HTTP status code.

    Works with both ``anthropic.APIStatusError`` and ``openai.APIStatusError``."""
    for exc in walk_exception_chain(e):
        code = getattr(exc, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def is_retryable_error(e: Exception) -> bool:
    """Determine whether the exception is transient (retry) or permanent.

    ``ToolTransportError`` is intentionally absent: its only raisers
    (tools/read_url.py, tools/providers/search.py) execute inside the
    ToolNode, whose ``handle_tool_errors`` converts it to an agent-visible
    error ToolMessage instead of re-raising — so the type can no longer reach
    this classifier, and keeping a "retryable" entry for it would contradict
    those agent-correctable semantics for any future raiser.
    """
    # Check exception type first (most reliable signal).
    if isinstance(e, McpToolCallError):
        return True
    if isinstance(e, (ConnectionError, TimeoutError)):
        return True
    # botocore timeouts: botocore.exceptions.ReadTimeoutError /
    # ConnectTimeoutError do NOT inherit from Python's builtin
    # TimeoutError (urllib3 defines its own same-named base). Import
    # lazily to avoid coupling the generic classifier to a specific
    # provider SDK at module-load time.
    try:
        from botocore.exceptions import ReadTimeoutError as _BotoReadTimeoutError
        from botocore.exceptions import ConnectTimeoutError as _BotoConnectTimeoutError

        if isinstance(e, (_BotoReadTimeoutError, _BotoConnectTimeoutError)):
            return True
    except ImportError:
        pass

    # Use HTTP status code from the provider exception if available
    status = extract_status_code(e)
    if status is not None:
        return status in RETRYABLE_STATUS_CODES

    # Fallback: string heuristics for errors without a status code
    error_str = str(e).lower()

    if "429" in error_str or "rate limit" in error_str or "rate exceeded" in error_str:
        return True
    if re.search(r'\b50[0234]\b', error_str):
        return True
    # Network-timeout phrasing produced by botocore / httpx / urllib3
    # when no HTTP status was received. Matches the exact prefixes
    # "Read timeout" and "Connect timeout" to avoid overmatching
    # unrelated error strings that happen to contain the word "timeout".
    if "read timeout" in error_str or "connect timeout" in error_str:
        return True
    if "validation" in error_str or "invalid" in error_str or "unsupported" in error_str or "pydantic" in error_str:
        return False
    if re.search(r'\b40[0-4]\b', error_str):
        return False

    # Default unknown exceptions to non-retryable
    return False
