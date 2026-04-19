<!-- AGENT_TASK_START: task-3-compaction-constants-and-thresholds.md -->

# Task 3 — Compaction Constants + Threshold Resolver

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — sections "Agent config extension" (platform-owned constants table), "Tier 1", "Tier 1.5", "Tier 3", and "Scale and operational plan".
2. `services/worker-service/executor/` directory structure — note how `memory_graph.py` co-locates related constants/types.
3. `services/worker-service/executor/memory_graph.py` — pattern for module-level constants, type-safe helpers, reducers.
4. `services/worker-service/core/config.py` — how the worker reads env vars and falls back to compiled-in defaults.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make worker-test`. Fix any regressions. New tests added here MUST pass.
2. Update the status in `docs/exec-plans/active/phase-2/track-7/progress.md` to "Done".

## Context

Track 7's pipeline and transforms need a shared set of platform constants (fractions, `KEEP_TOOL_USES`, per-result cap, truncatable-arg allowlist) and a single point of truth for resolving per-model thresholds. Centralising them in two small, well-tested modules lets all downstream tasks (3–8) import stable values rather than hard-coding magic numbers.

Threshold resolution is fraction-only in v1 — no absolute token cap. A 1M-context Gemini model gets a ~495K Tier 1 trigger; a 200K Sonnet model gets ~95K. A minimum-separation guardrail ensures Tier 3 always fires strictly above Tier 1 even on pathologically small models (8K).

## Task-Specific Shared Contract

- Constants live in `services/worker-service/executor/compaction/defaults.py`. Immutable module-level values; no `os.environ` reads at import time. Env-var overrides (if any) are read explicitly by the module function that consumes the constant, not at import time.
- Threshold resolver lives in `services/worker-service/executor/compaction/thresholds.py`. Pure, deterministic, no I/O. Exports a `Thresholds` `NamedTuple` and a `resolve_thresholds(model_context_window: int) -> Thresholds` function.
- Both modules MUST be importable with zero side-effects (no logger configuration at import, no DB connection, no env-var reading, no LangChain/LangGraph import at module scope beyond type-only imports).
- Constants and helpers are pure Python — no asyncio, no asyncpg, no network.

## Affected Component

- **Service/Module:** Worker Service — Compaction
- **File paths:**
  - `services/worker-service/executor/compaction/__init__.py` — **already created by Task 2** as a docstring-only file; Task 3 does NOT touch it. Task 8 owns re-exports.
  - `services/worker-service/executor/compaction/defaults.py` (new)
  - `services/worker-service/executor/compaction/thresholds.py` (new)
  - `services/worker-service/tests/test_compaction_defaults.py` (new)
  - `services/worker-service/tests/test_compaction_thresholds.py` (new)
- **Change type:** New package + new modules + unit tests

## Dependencies

- **Must complete first:** None.
- **Provides output to:** Task 4 (imports `PER_TOOL_RESULT_CAP_BYTES`), Task 5 (imports `KEEP_TOOL_USES`, `PLATFORM_EXCLUDE_TOOLS`), Task 6 (imports `TRUNCATABLE_TOOL_ARG_KEYS`, `ARG_TRUNCATION_CAP_BYTES`), Task 7 (imports `SUMMARIZER_MAX_RETRIES`, `PLATFORM_DEFAULT_SUMMARIZER_MODEL`, `get_platform_default_summarizer_model`), Task 8 (imports everything + calls `resolve_thresholds`; uses `TIER_3_MAX_FIRINGS_PER_TASK`).
- **Shared interfaces/contracts:** The set of module-level constants and the `Thresholds` type.

## Implementation Specification

### `defaults.py`

Define the following module-level constants with inline comments citing the design doc:

```python
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
```

Add a small helper:

```python
def get_platform_default_summarizer_model() -> str:
    """Return the platform-default summarizer model, honoring the env override.

    Reads os.environ lazily so tests and runtime overrides take effect.
    """
    import os
    return os.environ.get(
        PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV,
        PLATFORM_DEFAULT_SUMMARIZER_MODEL,
    )
```

Add a validation function at import time that sanity-checks the constants:

```python
assert 0 < TIER_1_TRIGGER_FRACTION < TIER_3_TRIGGER_FRACTION < 1.0
assert OUTPUT_BUDGET_RESERVE_TOKENS >= 0
assert MIN_TIER_SEPARATION_TOKENS > 0
assert KEEP_TOOL_USES >= 1
assert PER_TOOL_RESULT_CAP_BYTES > 0
assert ARG_TRUNCATION_CAP_BYTES > 0
assert SUMMARIZER_MAX_RETRIES >= 0
```

### `thresholds.py`

```python
"""Per-model threshold resolution for Track 7 compaction.

See docs/design-docs/phase-2/track-7-context-window-management.md §Agent config
extension for the threshold shape and model-size behavior.
"""
from typing import NamedTuple

from executor.compaction.defaults import (
    MIN_TIER_SEPARATION_TOKENS,
    OUTPUT_BUDGET_RESERVE_TOKENS,
    TIER_1_TRIGGER_FRACTION,
    TIER_3_TRIGGER_FRACTION,
)


class Thresholds(NamedTuple):
    tier1: int   # Tier 1 / Tier 1.5 trigger in estimated input tokens
    tier3: int   # Tier 3 trigger in estimated input tokens


def resolve_thresholds(model_context_window: int) -> Thresholds:
    """Compute Tier 1 and Tier 3 trigger thresholds for a given model.

    Thresholds are fraction-only in v1 — no absolute token cap. Customers
    picking large-context models get proportionally higher thresholds.

    A minimum-separation guardrail ensures Tier 3 fires strictly above Tier 1
    on pathologically small models.
    """
    if model_context_window <= 0:
        raise ValueError(
            f"model_context_window must be positive; got {model_context_window}"
        )
    effective_budget = max(0, model_context_window - OUTPUT_BUDGET_RESERVE_TOKENS)
    tier1 = int(effective_budget * TIER_1_TRIGGER_FRACTION)
    tier3 = int(effective_budget * TIER_3_TRIGGER_FRACTION)
    if tier3 - tier1 < MIN_TIER_SEPARATION_TOKENS:
        tier3 = tier1 + MIN_TIER_SEPARATION_TOKENS
    return Thresholds(tier1=tier1, tier3=tier3)
```

### `__init__.py`

Create a **minimal** `__init__.py` with only the package docstring — NO re-exports:

```python
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
```

**Why no re-exports here:** Tasks 2–6 each create separate modules; consolidating all re-exports into `__init__.py` would require every downstream task to edit the same file, creating merge conflicts when Tasks 3–6 run in parallel. Task 8 owns the final `__init__.py` shape as part of its integration work. Downstream tasks import from submodules directly.

## Acceptance Criteria

- [ ] Submodule imports succeed (worker-service cwd-based import root — same pattern as `from executor.graph import GraphExecutor`):
  - `from executor.compaction.defaults import KEEP_TOOL_USES`
  - `from executor.compaction.defaults import PER_TOOL_RESULT_CAP_BYTES`
  - `from executor.compaction.defaults import get_platform_default_summarizer_model`
  - `from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS` — MUST include `memory_note`, `save_memory`, `request_human_input`, `memory_search`, `task_history_get` (all five).
  - `from executor.compaction.thresholds import resolve_thresholds, Thresholds`
  Package-root imports (`from executor.compaction import ...`) are NOT required in this task. Task 8 owns `compaction/__init__.py` and adds the public re-exports there; downstream tasks (3–6) always import from submodules directly per the task-2 `__init__.py` docstring.
- [ ] `resolve_thresholds(200_000)` returns `Thresholds(tier1≈95_000, tier3≈142_500)` — exact values depend on `OUTPUT_BUDGET_RESERVE_TOKENS`, assert within tolerance.
- [ ] `resolve_thresholds(1_000_000)` returns `Thresholds(tier1≈495_000, tier3≈742_500)`.
- [ ] `resolve_thresholds(8_000)` returns a Thresholds value where `tier3 - tier1 >= MIN_TIER_SEPARATION_TOKENS`.
- [ ] `resolve_thresholds(0)` raises `ValueError`.
- [ ] `resolve_thresholds(-1)` raises `ValueError`.
- [ ] Running the module (via `services/worker-service/.venv/bin/python -c "import executor.compaction.defaults"` with cwd set to `services/worker-service`) produces no output and no errors (import-time assertions pass).
- [ ] `get_platform_default_summarizer_model()` returns `"claude-haiku-4-5"` with no env override; returns the override value when `CONTEXT_MGMT_DEFAULT_SUMMARIZER_MODEL=xyz` is set in `os.environ`.
- [ ] No import of LangChain / LangGraph / asyncpg / langfuse from `defaults.py` or `thresholds.py` — unit test asserts these modules are absent from the imported module object's graph.
- [ ] `make worker-test` — full worker unit suite passes.

## Testing Requirements

- **Unit tests for `defaults.py`:** import-time sanity-check assertions hold (tests may re-import the module after patching constants to verify the assertion guards catch a regression); env-var helper honors overrides.
- **Unit tests for `thresholds.py`:** table-driven coverage for 4K, 8K, 16K, 32K, 128K, 200K, 1M, 2M context windows; assertion on `tier3 > tier1 + MIN_TIER_SEPARATION_TOKENS - 1` (using `-1` to allow exact separation); negative/zero inputs raise `ValueError`.
- **Import-purity test:** asserting that importing the compaction package does NOT load LangChain, LangGraph, asyncpg, or langfuse (check via `sys.modules`).
- Use pytest conventions already present in `services/worker-service/tests/`.

## Constraints and Guardrails

- No env-var reads at module import time. The only exception is the lazy helper `get_platform_default_summarizer_model`.
- No logger config at import. No side-effecting imports.
- Do not expose per-agent override knobs in this task — constants are platform-owned.
- Do not wire these constants into `agent_node` or any graph — Task 8 does that.
- Do not add a cache / memoization to `resolve_thresholds` — it's trivially cheap and called at most once per LLM call. Premature optimization.
- Use `frozenset` for the tool-name allowlists so they cannot be mutated at runtime by a confused caller.

## Assumptions

- Python 3.11+ is the worker runtime (supports `NamedTuple` class syntax, `frozenset[str]` type hints).
- The `services.worker_service.executor.compaction` package path matches existing import conventions.
- `make worker-test` is invoked from the repo root and uses the pinned `.venv` per `CLAUDE.md`.

<!-- AGENT_TASK_END: task-3-compaction-constants-and-thresholds.md -->
