"""Platform-owned compaction constants (Track 7, Tier 0 through Tier 3).

All thresholds are platform-owned and NOT exposed via the public API.
Promoting any to per-agent configuration requires production telemetry
justifying the knob — see docs/design-docs/phase-2/track-7-context-window-management.md
§ "Platform-owned defaults with narrow per-agent tuning knobs".

References:
- Anthropic Cookbook — "Context engineering: memory, compaction, and tool clearing" (March 2026).
- JetBrains Research + "The Complexity Trap" (arXiv 2508.21433, NeurIPS DL4C 2025) — masking
  beats summarization on both cost and solve rate in 4 of 5 settings.
- Manus — "Context Engineering for AI Agents" (July 2025) — KV-cache preservation as dominant
  cost lever.
"""

# ---------------------------------------------------------------------------
# Tier 0 — Per-tool-result cap at ingestion
# ---------------------------------------------------------------------------

# Hard byte cap applied to every ToolMessage at ingestion. Byte-level (not
# token-level) for cache-stability — the capped string is written to state,
# checkpointed, and replayed without further transformation.
# Value: 25 000 bytes ≈ 25 KB. Chosen to allow ~500 tool uses × 25 KB ≈ 12.5 MB
# raw content on a 200 K-context model, which Tier 1 masking then collapses to
# ~60 KB of placeholders.
PER_TOOL_RESULT_CAP_BYTES: int = 25_000

# ---------------------------------------------------------------------------
# Tier 1 + 1.5 — Observation masking and argument truncation triggers
# ---------------------------------------------------------------------------

# Fraction of effective budget at which Tier 1 (masking) and Tier 1.5 (arg
# truncation) activate. "Effective budget" = model_context_window −
# OUTPUT_BUDGET_RESERVE_TOKENS.
TIER_1_TRIGGER_FRACTION: float = 0.50

# Number of most-recent tool-use turns that are always kept intact (Tier 1
# protection window). Masking only touches turns *before* this window.
KEEP_TOOL_USES: int = 3

# Arg keys in AIMessage.tool_calls that carry large string payloads and are
# eligible for truncation at Tier 1.5 (e.g., sandbox_write_file.content).
TRUNCATABLE_TOOL_ARG_KEYS: frozenset[str] = frozenset(
    {"content", "new_string", "old_string", "text", "body"}
)

# An arg value longer than this (bytes) in an older-than-protection-window
# turn is replaced with "[<n> bytes — arg truncated after step i]".
ARG_TRUNCATION_CAP_BYTES: int = 1_000

# Tools whose *results* must never be masked by Tier 1. Platform-seeded list;
# agents can extend via context_management.exclude_tools.
PLATFORM_EXCLUDE_TOOLS: frozenset[str] = frozenset(
    {
        "memory_note",
        "save_memory",
        "request_human_input",
        "memory_search",
        "task_history_get",
    }
)

# ---------------------------------------------------------------------------
# Tier 3 — Retrospective LLM summarization
# ---------------------------------------------------------------------------

# Fraction of effective budget at which Tier 3 (LLM summarization) fires,
# only after Tier 1 + 1.5 cannot bring input below this threshold.
TIER_3_TRIGGER_FRACTION: float = 0.75

# Minimum token separation between Tier 1 and Tier 3 thresholds. Guards
# against tiny-context models where the fraction terms collapse together.
MIN_TIER_SEPARATION_TOKENS: int = 2_000

# Maximum summarizer LLM retry attempts before Tier 3 is skipped for this call
# and re-attempted on the next agent-node call.
SUMMARIZER_MAX_RETRIES: int = 2

# ---------------------------------------------------------------------------
# Output budget / context window
# ---------------------------------------------------------------------------

# Tokens reserved from model_context_window for the model's response. Subtracted
# when computing the "effective budget" for Tier 1 / Tier 3 triggers.
OUTPUT_BUDGET_RESERVE_TOKENS: int = 10_000
