"""Platform-owned compaction constants for Track 7 context window management.

These constants are referenced in:
  - Anthropic Cookbook "Context engineering: memory, compaction, and tool clearing"
    (March 2026) — trigger-fraction values and keep-count.
  - JetBrains Research / NeurIPS DL4C 2025 (arXiv 2508.21433) — empirical support
    for observation masking beating LLM summarisation on cost and solve rate.
  - Cognition "Don't Build Multi-Agents" (June 2025) — silent-compaction rule.

**None of these are exposed via the public API.** Promoting any of them to a
per-agent knob requires production telemetry justifying the change.

See ``docs/design-docs/phase-2/track-7-context-window-management.md`` §Platform-owned
for the full rationale and future-promotion criteria.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Tier thresholds
# ---------------------------------------------------------------------------

# Fraction of the model's effective context budget at which Tier 1 masking fires.
# Source: Anthropic Cookbook recommendation of 50 % threshold as a safe default
# that avoids premature masking while still providing a large safety margin.
TIER_1_TRIGGER_FRACTION: float = 0.50

# Fraction at which Tier 3 LLM summarisation fires (after Tier 1 + 1.5 run).
# Source: Anthropic Cookbook; set at 75 % to ensure Tier 1 runs first with
# substantial headroom (25 pp gap on 200K Sonnet = ~50K tokens).
TIER_3_TRIGGER_FRACTION: float = 0.75

# Token headroom reserved for the model's own response. Subtracted from
# model_context_window before computing effective budget and trigger thresholds.
OUTPUT_BUDGET_RESERVE_TOKENS: int = 10_000

# Minimum token separation between Tier 1 and Tier 3 triggers. Guards against
# pathologically small models (e.g., 8K context) where the fraction terms
# collapse to the same value.
MIN_TIER_SEPARATION_TOKENS: int = 2_000

# ---------------------------------------------------------------------------
# Tier 1 / 1.5 parameters
# ---------------------------------------------------------------------------

# Most recent tool-use turns kept intact (not masked) by Tier 1.
# Source: Anthropic Cookbook "keep=3" default.
KEEP_TOOL_USES: int = 3

# Hard cap on a single ToolMessage payload (byte-count, not token-count).
# Applied at ingestion (tool-execution wrapper) before the message enters state.
# Head+tail split mirrors Claude Code's exec truncation.
PER_TOOL_RESULT_CAP_BYTES: int = 25_000

# Tool-call argument keys that Tier 1.5 truncates in older AIMessage records.
# Targets the most common large-payload offender (sandbox_write_file.content)
# and its siblings. Adding a new key (e.g., ``patch``) requires a one-line
# update here — not a per-agent config change.
TRUNCATABLE_TOOL_ARG_KEYS: frozenset[str] = frozenset(
    {"content", "new_string", "old_string", "text", "body"}
)

# Maximum byte length for a truncatable arg. Args longer than this, in a turn
# behind the protection window, are replaced with ``[<n> bytes]``.
ARG_TRUNCATION_CAP_BYTES: int = 1_000

# ---------------------------------------------------------------------------
# Platform exclude-tools list (Tier 1 — never mask these tool results)
# ---------------------------------------------------------------------------

# Tools whose ToolMessage results must NEVER be masked by Tier 1, regardless
# of position in history.  The agent may need these even dozens of turns later.
#
#   memory_note / save_memory  — durable agent observations
#   request_human_input        — the task pivot; masking it erases the reason
#                                the agent paused
#   memory_search              — the agent explicitly fetched this to inform
#                                the current task; masking defeats the fetch
#   task_history_get           — same rationale as memory_search
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
# Tier 3 — summariser parameters
# ---------------------------------------------------------------------------

# Number of retry attempts after an initial failure. 2 retries = 3 total
# attempts before Tier 3 is skipped for this call.
# Source: Anthropic Cookbook default retry guidance for cheap-model calls.
SUMMARIZER_MAX_RETRIES: int = 2

# Default summariser model. Overridable per-agent via
# ``agent_config.context_management.summarizer_model``.
# Uses the env-var escape hatch so test suites can swap it out without
# touching DB rows.
PLATFORM_DEFAULT_SUMMARIZER_MODEL: str = os.environ.get(
    "PLATFORM_DEFAULT_SUMMARIZER_MODEL",
    "claude-haiku-4-5",
)


def get_platform_default_summarizer_model() -> str:
    """Return the effective platform-default summariser model ID.

    Reads from ``PLATFORM_DEFAULT_SUMMARIZER_MODEL`` env var at call time so
    test overrides (``monkeypatch.setenv``) are honoured without module reload.
    """
    return os.environ.get(
        "PLATFORM_DEFAULT_SUMMARIZER_MODEL",
        "claude-haiku-4-5",
    )
