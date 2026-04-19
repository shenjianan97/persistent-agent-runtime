"""Compaction pipeline for Track 7 context window management.

This package provides the tiered compaction transforms that run inside
the LangGraph executor loop to keep per-step token counts within budget:

- Tier 1 (``transforms.clear_tool_results``) — tool-result masking
- Tier 1.5 (``transforms.truncate_tool_call_args``) — argument truncation
- Tier 3 (``summarizer.summarize_slice``) — retrospective LLM summarisation

The top-level ``compact_for_llm`` pipeline is assembled in Task 8
(``executor.graph`` integration).

Do NOT import the pipeline entry point here until Task 8 is complete.
"""
