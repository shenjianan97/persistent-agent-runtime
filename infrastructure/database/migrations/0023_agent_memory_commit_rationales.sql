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
-- ``GIN`` index mirrors the one already present for ``observations`` so
-- future search work can include rationales without a new migration.

ALTER TABLE agent_memory_entries
    ADD COLUMN commit_rationales TEXT[];

CREATE INDEX IF NOT EXISTS idx_agent_memory_commit_rationales
    ON agent_memory_entries USING GIN (commit_rationales);
