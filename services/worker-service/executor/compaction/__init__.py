"""Track 7 — Context Window Management.

This package houses the compaction and summary-marker logic introduced in
Phase 2 Track 7. Task 2 seeded it with the shared state schema
(:mod:`executor.compaction.state`). Task 8 wires the full pipeline and
exports the unified public API from this package root.

See docs/design-docs/phase-2/track-7-context-window-management.md.

Public API (Task 8 consolidated)
---------------------------------
State schema:
    RuntimeState, _max_reducer, _any_reducer,
    _summary_marker_strict_append_reducer

Platform defaults:
    KEEP_TOOL_USES, PLATFORM_EXCLUDE_TOOLS, TRUNCATABLE_ARG_KEYS,
    TRUNCATABLE_TOOL_ARG_KEYS (alias), ARG_TRUNCATION_CAP_BYTES,
    OFFLOAD_THRESHOLD_BYTES, TIER_3_MAX_FIRINGS_PER_TASK,
    get_platform_default_summarizer_model

Threshold resolution:
    resolve_thresholds, Thresholds

Tier 1 transforms:
    clear_tool_results, ClearResult

Tier 0 ingestion offload (Track 7 Follow-up, Task 4):
    offload_tool_message, offload_ai_message_args, OffloadOutcome,
    OffloadEvent, ToolResultArtifactStore, S3ToolResultStore,
    InMemoryToolResultStore, parse_tool_result_uri, ToolResultURI

Tier 3 summarizer:
    summarize_slice, SummarizeResult

Pipeline orchestrator:
    compact_for_llm, CompactionPassResult
    HardFloorEvent, Tier1AppliedEvent, Tier15AppliedEvent,
    Tier3FiredEvent, Tier3SkippedEvent, MemoryFlushFiredEvent

Token estimation:
    estimate_tokens
"""

# ---------------------------------------------------------------------------
# State schema + reducers
# ---------------------------------------------------------------------------
from executor.compaction.state import (
    RuntimeState,
    _any_reducer,
    _max_reducer,
    _summary_marker_strict_append_reducer,
)

# ---------------------------------------------------------------------------
# Platform defaults
# ---------------------------------------------------------------------------
from executor.compaction.defaults import (
    ARG_TRUNCATION_CAP_BYTES,
    KEEP_TOOL_USES,
    OFFLOAD_THRESHOLD_BYTES,
    PLATFORM_DEFAULT_SUMMARIZER_MODEL,
    PLATFORM_EXCLUDE_TOOLS,
    SUMMARIZER_MAX_RETRIES,
    TIER_3_MAX_FIRINGS_PER_TASK,
    TRUNCATABLE_ARG_KEYS,
    TRUNCATABLE_TOOL_ARG_KEYS,
    get_platform_default_summarizer_model,
)

# ---------------------------------------------------------------------------
# Threshold resolution
# ---------------------------------------------------------------------------
from executor.compaction.thresholds import Thresholds, resolve_thresholds

# ---------------------------------------------------------------------------
# Tier 1 transforms
# ---------------------------------------------------------------------------
from executor.compaction.transforms import (
    ClearResult,
    clear_tool_results,
)

# ---------------------------------------------------------------------------
# Tier 0 ingestion offload (Track 7 Follow-up, Task 4)
# ---------------------------------------------------------------------------
from executor.compaction.ingestion import (
    OffloadEvent,
    OffloadOutcome,
    offload_ai_message_args,
    offload_tool_message,
    offload_tool_messages_batch,
)
from executor.compaction.tool_result_store import (
    InMemoryToolResultStore,
    S3ToolResultStore,
    ToolResultArtifactStore,
    ToolResultURI,
    parse_tool_result_uri,
)

# ---------------------------------------------------------------------------
# Tier 3 summarizer
# ---------------------------------------------------------------------------
from executor.compaction.summarizer import SummarizeResult, summarize_slice

# ---------------------------------------------------------------------------
# Pipeline orchestrator (Task 8)
# ---------------------------------------------------------------------------
from executor.compaction.pipeline import (
    CompactionPassResult,
    HardFloorEvent,
    MemoryFlushFiredEvent,
    Tier1AppliedEvent,
    Tier15AppliedEvent,
    Tier3FiredEvent,
    Tier3SkippedEvent,
    compact_for_llm,
)

# ---------------------------------------------------------------------------
# Token estimation (Task 8)
# ---------------------------------------------------------------------------
from executor.compaction.tokens import estimate_tokens

__all__ = [
    # State schema
    "RuntimeState",
    "_max_reducer",
    "_any_reducer",
    "_summary_marker_strict_append_reducer",
    # Defaults
    "KEEP_TOOL_USES",
    "OFFLOAD_THRESHOLD_BYTES",
    "PLATFORM_EXCLUDE_TOOLS",
    "TRUNCATABLE_ARG_KEYS",
    "TRUNCATABLE_TOOL_ARG_KEYS",
    "ARG_TRUNCATION_CAP_BYTES",
    "TIER_3_MAX_FIRINGS_PER_TASK",
    "PLATFORM_DEFAULT_SUMMARIZER_MODEL",
    "SUMMARIZER_MAX_RETRIES",
    "get_platform_default_summarizer_model",
    # Thresholds
    "Thresholds",
    "resolve_thresholds",
    # Transforms
    "ClearResult",
    "clear_tool_results",
    # Ingestion offload (Track 7 Follow-up, Task 4)
    "OffloadEvent",
    "OffloadOutcome",
    "offload_ai_message_args",
    "offload_tool_message",
    "offload_tool_messages_batch",
    "ToolResultArtifactStore",
    "ToolResultURI",
    "InMemoryToolResultStore",
    "S3ToolResultStore",
    "parse_tool_result_uri",
    # Summarizer
    "SummarizeResult",
    "summarize_slice",
    # Pipeline
    "CompactionPassResult",
    "HardFloorEvent",
    "MemoryFlushFiredEvent",
    "Tier1AppliedEvent",
    "Tier15AppliedEvent",
    "Tier3FiredEvent",
    "Tier3SkippedEvent",
    "compact_for_llm",
    # Tokens
    "estimate_tokens",
]
