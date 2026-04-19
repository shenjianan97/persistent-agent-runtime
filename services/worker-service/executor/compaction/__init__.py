"""Track 7 — Context Window Management.

See docs/design-docs/phase-2/track-7-context-window-management.md.

Public API is re-exported from this package in Task 8 (pipeline integration).
Earlier tasks (2–6) import directly from submodules:

    from executor.compaction.defaults import KEEP_TOOL_USES
    from executor.compaction.thresholds import resolve_thresholds, Thresholds
    from executor.compaction.caps import cap_tool_result, CapEvent
    from executor.compaction.transforms import clear_tool_results, ClearResult
    from executor.compaction.transforms import truncate_tool_call_args, TruncateResult
    from executor.compaction.summarizer import summarize_slice, SummarizeResult
"""
