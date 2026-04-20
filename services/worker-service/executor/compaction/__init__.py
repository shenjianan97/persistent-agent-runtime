"""Track 7 + Track 7 Follow-up — Context Window Management.

This package houses the compaction and summary logic introduced in Phase 2
Track 7 and reshaped by the Track 7 Follow-up (Task 3 replace-and-rehydrate).

Public API
----------
State schema:
    RuntimeState, _max_reducer, _any_reducer, _summary_replace_reducer

Platform defaults:
    KEEP_TOOL_USES, PLATFORM_EXCLUDE_TOOLS, TRUNCATABLE_ARG_KEYS,
    TRUNCATABLE_TOOL_ARG_KEYS (back-compat alias), ARG_TRUNCATION_CAP_BYTES,
    OFFLOAD_THRESHOLD_BYTES, COMPACTION_TRIGGER_FRACTION,
    TIER_3_MAX_FIRINGS_PER_TASK, SUMMARIZER_MAX_OUTPUT_TOKENS,
    SUMMARIZER_INPUT_HEADROOM_TOKENS, get_platform_default_summarizer_model

Threshold resolution (legacy Track 7 — retained for tests):
    resolve_thresholds, Thresholds

Tier 0 ingestion offload (Task 4):
    offload_tool_message, offload_ai_message_args, OffloadOutcome,
    OffloadEvent, ToolResultArtifactStore, S3ToolResultStore,
    InMemoryToolResultStore, parse_tool_result_uri, ToolResultURI

Summariser:
    summarize_slice, SummarizeResult

``pre_model_hook`` orchestrator (Task 3 — replaces Track 7's ``compact_for_llm``):
    compaction_pre_model_hook, CompactionPassResult,
    find_keep_window_start, should_fire_pre_tier3_flush,
    HardFloorEvent, Tier3FiredEvent, Tier3SkippedEvent, MemoryFlushFiredEvent

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
    _summary_replace_reducer,
)

# ---------------------------------------------------------------------------
# Platform defaults
# ---------------------------------------------------------------------------
from executor.compaction.defaults import (
    ARG_TRUNCATION_CAP_BYTES,
    COMPACTION_TRIGGER_FRACTION,
    KEEP_TOOL_USES,
    OFFLOAD_THRESHOLD_BYTES,
    PLATFORM_DEFAULT_SUMMARIZER_MODEL,
    PLATFORM_EXCLUDE_TOOLS,
    SUMMARIZER_INPUT_HEADROOM_TOKENS,
    SUMMARIZER_MAX_OUTPUT_TOKENS,
    SUMMARIZER_MAX_RETRIES,
    TIER_3_MAX_FIRINGS_PER_TASK,
    TRUNCATABLE_ARG_KEYS,
    TRUNCATABLE_TOOL_ARG_KEYS,
    get_platform_default_summarizer_model,
)

# ---------------------------------------------------------------------------
# Threshold resolution (legacy Track 7; retained for back-compat with tests)
# ---------------------------------------------------------------------------
from executor.compaction.thresholds import Thresholds, resolve_thresholds

# ---------------------------------------------------------------------------
# Tier 0 ingestion offload (Task 4)
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
# Summariser
# ---------------------------------------------------------------------------
from executor.compaction.summarizer import SummarizeResult, summarize_slice

# ---------------------------------------------------------------------------
# pre_model_hook orchestrator (Track 7 Follow-up, Task 3)
# ---------------------------------------------------------------------------
from executor.compaction.pre_model_hook import (
    CompactionPassResult,
    HardFloorEvent,
    MemoryFlushFiredEvent,
    Tier3FiredEvent,
    Tier3SkippedEvent,
    compaction_pre_model_hook,
    find_keep_window_start,
    option_c_reference_replacement,
    should_fire_pre_tier3_flush,
)

# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------
from executor.compaction.tokens import estimate_tokens

__all__ = [
    # State schema
    "RuntimeState",
    "_max_reducer",
    "_any_reducer",
    "_summary_replace_reducer",
    # Defaults
    "COMPACTION_TRIGGER_FRACTION",
    "KEEP_TOOL_USES",
    "OFFLOAD_THRESHOLD_BYTES",
    "PLATFORM_EXCLUDE_TOOLS",
    "TRUNCATABLE_ARG_KEYS",
    "TRUNCATABLE_TOOL_ARG_KEYS",
    "ARG_TRUNCATION_CAP_BYTES",
    "TIER_3_MAX_FIRINGS_PER_TASK",
    "PLATFORM_DEFAULT_SUMMARIZER_MODEL",
    "SUMMARIZER_MAX_RETRIES",
    "SUMMARIZER_MAX_OUTPUT_TOKENS",
    "SUMMARIZER_INPUT_HEADROOM_TOKENS",
    "get_platform_default_summarizer_model",
    # Thresholds
    "Thresholds",
    "resolve_thresholds",
    # Ingestion offload
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
    # Summariser
    "SummarizeResult",
    "summarize_slice",
    # pre_model_hook
    "CompactionPassResult",
    "HardFloorEvent",
    "MemoryFlushFiredEvent",
    "Tier3FiredEvent",
    "Tier3SkippedEvent",
    "compaction_pre_model_hook",
    "find_keep_window_start",
    "option_c_reference_replacement",
    "should_fire_pre_tier3_flush",
    # Tokens
    "estimate_tokens",
]
