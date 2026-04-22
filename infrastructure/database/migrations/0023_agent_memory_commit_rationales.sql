-- 0023: Separate commit rationales from observations on agent_memory_entries.
--
-- Before this migration, ``save_memory``'s reason was folded into the same
-- ``observations`` TEXT[] channel as ``note_finding`` (prefixed with
-- "[save_memory] "). The two have different semantics — observations are
-- what the agent *learned*, rationales are why the agent *chose to save
-- this run* — and mixing them muddles the detail UI and search corpus.
-- Issue #102.
--
-- This migration adds a nullable ``commit_rationales TEXT[]`` column.
-- New writes populate it from the graph's ``commit_rationales`` state
-- channel; older rows keep their legacy "[save_memory] ..." entries inside
-- ``observations`` (there is no in-place backfill — see design-doc
-- trade-off: cheap forward-only cleanup over risky bulk UPDATE on a
-- multi-tenant shared table). Read paths treat NULL and empty-list the
-- same way.
--
-- No GIN index on ``commit_rationales`` is created here. The
-- ``agent_memory_entries`` full-text search corpus goes through the
-- ``content_tsv`` generated column (see migration 0011), and extending
-- that generated column requires a full-table rewrite — deferred to a
-- follow-up migration so this change stays surgical.  If search recall
-- over rationales becomes valuable, rebuild ``content_tsv`` to include
-- ``immutable_array_to_string(commit_rationales)`` in one atomic step,
-- which will create the corresponding GIN entry automatically; a raw
-- GIN on the array would pay index maintenance cost for zero reader
-- benefit today.

ALTER TABLE agent_memory_entries
    ADD COLUMN commit_rationales TEXT[];
