-- Phase 2 Track 5: Defense-in-depth CHECK constraints on agent_memory_entries.
--
-- Rationale: the tool layer caps memory writes at lower values
-- (``memory_note`` enforces 2 KB per observation; the retrospective summarizer
-- targets ~4 KB). These caps live in application code (see
-- ``services/worker-service/tools/memory_tools.py`` and the memory-write path)
-- and an application bug, future refactor, or crafted write path could bypass
-- them. This migration adds outer-envelope DB-level bounds so an unbounded
-- blob cannot land in ``agent_memory_entries`` even on a wayward write path.
--
-- The values here are intentionally wider than the tool-layer caps so that
-- legitimate format evolution (slightly longer summaries, a new observation
-- style) does not require a migration, while still capping worst-case storage
-- per row at O(tens of KB).
--
-- Existing column bounds already in 0011:
--   - ``title`` CHECK (length(title) <= 200)
--   - ``outcome`` CHECK (outcome IN ('succeeded', 'failed'))
--
-- New outer bounds added here:
--   - ``summary`` length <= 8192 (2x design-expected ~4 KB; format headroom)
--   - ``observations`` cardinality <= 1000 (an agent emitting >1000 notes for
--     a single task has bigger problems)
--   - per-element ``observations`` length <= 4096 (2x the 2 KB tool cap)
--
-- Postgres CHECK constraints must be IMMUTABLE and cannot use subqueries, but
-- they CAN call IMMUTABLE SQL helper functions. Migration 0011 already uses
-- this pattern (see ``immutable_array_to_string``) for the same reason, so the
-- per-element cap is implemented via a small helper rather than skipped.
--
-- Safety: This migration is ADDITIVE. Existing rows at the time of migration
-- are re-validated on commit — if any row violates the new bounds, the
-- migration FAILS and the transaction rolls back. That is the intended
-- behaviour: a pre-existing row over the cap is a real data-quality signal,
-- not an operator problem to work around. Current deployments have no such
-- rows (the tool-layer caps at 2 KB / per-call prevent them).

-- Step 1: IMMUTABLE helper for per-element length — returns the max length of
-- any element in the array (0 for NULL/empty). Composed entirely of IMMUTABLE
-- primitives so it can be safely marked IMMUTABLE and referenced from a CHECK.
CREATE OR REPLACE FUNCTION immutable_array_max_element_length(arr TEXT[])
RETURNS INT
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT coalesce(max(length(elem)), 0)
    FROM unnest(arr) AS elem;
$$;

-- Step 2: Add defense-in-depth CHECK constraints. Names are explicit so they
-- show up cleanly in error messages (``asyncpg.CheckViolationError`` exposes
-- the constraint name) and so future migrations can drop/adjust them by name.

ALTER TABLE agent_memory_entries
    ADD CONSTRAINT agent_memory_entries_summary_length_chk
    CHECK (length(summary) <= 8192);

ALTER TABLE agent_memory_entries
    ADD CONSTRAINT agent_memory_entries_observations_cardinality_chk
    CHECK (cardinality(observations) <= 1000);

ALTER TABLE agent_memory_entries
    ADD CONSTRAINT agent_memory_entries_observation_element_length_chk
    CHECK (immutable_array_max_element_length(observations) <= 4096);
