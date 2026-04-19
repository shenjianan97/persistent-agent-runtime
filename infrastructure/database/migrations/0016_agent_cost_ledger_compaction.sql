-- 0016: Extend agent_cost_ledger for Track 7 compaction attribution.
-- Adds: operation (what spent the money), model_id (which model), tokens_in/out
-- (for reconciliation), and a unique key so crash-after-insert-before-state
-- replay is swallowed by ON CONFLICT DO NOTHING rather than double-charging.
--
-- Existing callers (model_token_spend, sandbox_runtime) keep working because
-- new columns all have NOT NULL DEFAULT values or allow NULL.

ALTER TABLE agent_cost_ledger
    ADD COLUMN operation TEXT NOT NULL DEFAULT 'model_token_spend'
        CHECK (operation IN (
            'model_token_spend',
            'sandbox_runtime',
            'memory_write',
            'compaction.tier3'
        )),
    ADD COLUMN model_id TEXT,
    ADD COLUMN tokens_in BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN tokens_out BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN summarized_through_turn_index_after INT;

-- Partial unique index — idempotency key for compaction.tier3 only.
-- The existing model_token_spend / sandbox paths already rely on application-
-- level dedup and must NOT be constrained here (would break backfill of
-- in-flight rows). Index is partial to keep it scoped to Track 7.
CREATE UNIQUE INDEX idx_agent_cost_ledger_tier3_idempotency
    ON agent_cost_ledger (tenant_id, task_id, checkpoint_id,
                          operation, summarized_through_turn_index_after)
    WHERE operation = 'compaction.tier3';
