"""Platform-owned constants for Track 7 compaction.

See docs/design-docs/phase-2/track-7-context-window-management.md for rationale.
All values are immutable; promoting any to per-agent config requires a deliberate
design decision backed by production telemetry.
"""

# Fraction of the model's effective budget (context_window - output_reserve) at
# which Tier 1 (tool-result clearing) starts firing. Applies to every LLM call.
TIER_1_TRIGGER_FRACTION: float = 0.50

# Fraction of the model's effective budget at which Tier 3 (retrospective LLM
# summarization) fires. Must be strictly greater than TIER_1_TRIGGER_FRACTION.
TIER_3_TRIGGER_FRACTION: float = 0.75

# Tokens reserved for the model's response. Subtracted from the model context
# window when computing the effective budget.
OUTPUT_BUDGET_RESERVE_TOKENS: int = 10_000

# Minimum gap (in tokens) enforced between Tier 1 and Tier 3 triggers on tiny-
# context models. Without this, 8K-context models can collapse both tiers to
# the same value.
MIN_TIER_SEPARATION_TOKENS: int = 2_000

# Most recent tool-use turns kept intact (never cleared by Tier 1).
KEEP_TOOL_USES: int = 3

# Hard byte cap enforced at tool-result ingestion (head + tail truncation).
# Measured in bytes (not tokens) because it applies at tool-execution time
# before any tokenization. 25,000 bytes ≈ 6–8K tokens on most tokenizers.
PER_TOOL_RESULT_CAP_BYTES: int = 25_000

# Tool-call argument keys subject to Tier 1.5 truncation. Agents rarely need to
# re-read their own inputs once the tool has executed.
TRUNCATABLE_TOOL_ARG_KEYS: frozenset[str] = frozenset({
    "content",
    "new_string",
    "old_string",
    "text",
    "body",
})

# Byte threshold above which a truncatable argument in an older turn is
# replaced with "[N bytes — arg truncated after step K]".
ARG_TRUNCATION_CAP_BYTES: int = 1_000

# Tools whose ToolMessage results are NEVER cleared by Tier 1 regardless of
# age. These are load-bearing across many turns. Customer agents can extend
# this list via agent_config.context_management.exclude_tools.
PLATFORM_EXCLUDE_TOOLS: frozenset[str] = frozenset({
    "memory_note",
    "save_memory",
    "request_human_input",
    # Memory-retrieval results: the agent *just explicitly fetched* these to
    # inform the current task. Clearing them once they age out of the keep
    # window defeats the fetch. See design doc §Tier 1: tool-result clearing.
    "memory_search",
    "task_history_get",
})

# Maximum retries for the Tier 3 summarizer LLM call before giving up on this
# pass. Giving up does NOT escalate to dead-letter; the next agent-node call
# re-attempts if the threshold is still exceeded.
SUMMARIZER_MAX_RETRIES: int = 2

# Hard cap on the Tier 3 summarizer's output tokens, forwarded to the LLM as
# ``max_tokens``. Acts as a safety net when a model ignores the prompt-level
# ≤500-token budget. 1500 gives a well-behaved model headroom to wrap up
# gracefully while capping a pathological runaway at ~3× the target.
#
# Sized for today's Track 7 workload (Tier-1-stubbed input). Under the Track 7
# Follow-up Task 3 replace-and-rehydrate rewrite the summarizer sees raw
# ``prior_summary + middle`` — 10-50× larger input — so legitimately longer
# summaries are expected; re-calibrate upward if truncation WARN rate exceeds
# 5% of firings post-Task 3.
SUMMARIZER_MAX_OUTPUT_TOKENS: int = 1500

# Maximum number of Tier 3 firings allowed per task. Beyond this cap the
# pipeline stops invoking the summarizer and falls through to the hard-floor
# path if the input still exceeds the floor. Bounds worst-case cost for
# pathological tasks (long-running agent with tight protection window + large
# exclude_tools). 10 firings × ~400-word summary × typical 20K-token slice
# roughly $0.50 at current Sonnet summarizer pricing.
TIER_3_MAX_FIRINGS_PER_TASK: int = 10

# Platform-default summarizer model when agent_config.context_management
# .summarizer_model is unset. Resolved per-call; not cached.
PLATFORM_DEFAULT_SUMMARIZER_MODEL: str = "claude-haiku-4-5"

# Env-var override for the platform-default summarizer. Read lazily by the
# pipeline at invocation time via get_platform_default_summarizer_model().
PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV: str = "CONTEXT_MGMT_DEFAULT_SUMMARIZER_MODEL"

# ---------------------------------------------------------------------------
# Sanity checks — enforced at import time to catch accidental constant edits.
# ---------------------------------------------------------------------------
assert 0 < TIER_1_TRIGGER_FRACTION < TIER_3_TRIGGER_FRACTION < 1.0
assert OUTPUT_BUDGET_RESERVE_TOKENS >= 0
assert MIN_TIER_SEPARATION_TOKENS > 0
assert KEEP_TOOL_USES >= 1
assert PER_TOOL_RESULT_CAP_BYTES > 0
assert ARG_TRUNCATION_CAP_BYTES > 0
assert SUMMARIZER_MAX_RETRIES >= 0
assert SUMMARIZER_MAX_OUTPUT_TOKENS > 0


def get_platform_default_summarizer_model() -> str:
    """Return the platform-default summarizer model, honoring the env override.

    Reads os.environ lazily so tests and runtime overrides take effect.
    """
    import os

    return os.environ.get(
        PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV,
        PLATFORM_DEFAULT_SUMMARIZER_MODEL,
    )
