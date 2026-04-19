"""Track 7 — Context Window Management.

This package houses the compaction and summary-marker logic introduced in
Phase 2 Track 7. Task 2 seeded it with the shared state schema
(:mod:`executor.compaction.state`). Subsequent tasks in Track 7 will
populate this package with the compaction trigger, the summarisation node,
and related helpers.

See docs/design-docs/phase-2/track-7-context-window-management.md.

Public API is re-exported from this package in Task 8 (pipeline integration).
Earlier tasks (2–6) import directly from submodules:

    from executor.compaction.state import RuntimeState
    from executor.compaction.defaults import KEEP_TOOL_USES
    from executor.compaction.thresholds import resolve_thresholds, Thresholds
    from executor.compaction.caps import cap_tool_result, CapEvent
    from executor.compaction.transforms import clear_tool_results, ClearResult
    from executor.compaction.transforms import truncate_tool_call_args, TruncateResult
    from executor.compaction.summarizer import summarize_slice, SummarizeResult
"""
