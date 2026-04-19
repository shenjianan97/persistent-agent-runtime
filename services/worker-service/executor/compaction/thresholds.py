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
    tier1: int  # Tier 1 / Tier 1.5 trigger in estimated input tokens
    tier3: int  # Tier 3 trigger in estimated input tokens


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
