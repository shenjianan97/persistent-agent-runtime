-- Phase 2 Track 7 Task 1: add context_window token count to models table.
-- Used by API validation to enforce that a chosen context_management.summarizer_model
-- has a context_window large enough to hold the primary model's Tier 3 trigger.
-- NULL means "unknown" (older seed rows); ConfigValidationHelper skips the check
-- gracefully when either the summarizer or primary window is NULL.
ALTER TABLE models ADD COLUMN IF NOT EXISTS context_window INTEGER;

COMMENT ON COLUMN models.context_window IS
    'Maximum context window in tokens for this model. NULL = unknown / not yet seeded. '
    'Used by the API to validate that context_management.summarizer_model can hold '
    'the primary model''s Tier 3 trigger (Track 7 Task 1). '
    'Platform constants: effective_budget = context_window - 10_000; '
    'tier3_trigger = int(effective_budget * 0.75).';
