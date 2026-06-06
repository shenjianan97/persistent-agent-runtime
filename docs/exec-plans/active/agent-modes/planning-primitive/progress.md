# Agent Modes — Planning Primitive — Progress

Status tracking for the Planning Primitive track ([plan.md](./plan.md)).
Sibling track: [Supervisor Topology](../supervisor-topology/progress.md).
Statuses: `Not started` · `In progress` · `Done` · `Blocked`.

| Task | Description | Status | Note |
|---|---|---|---|
| P1 | `RuntimeState.plan` (full-replace reducer) + `plan_write` tool + v1 caps | Done | spec + quality reviewed (approved 2026-06-06); 43 unit tests; review fixes: required `items`, `PlanItem` schema wired, typed errors, duplicate-id rejection |
| P2 | Post-compaction plan injection as a neutral Markdown `SystemMessage` | In progress | depends on P1 |
| P3 | `GET /v1/tasks/{id}/plan` read-only projection | Done | spec + quality reviewed (approved 2026-06-06); 12 new Java tests; added scope delivered: `plan_write` in `ValidationConstants.ALLOWED_TOOLS`; refactor: checkpoint-payload parsing deduped into `JsonParseUtil.parseJsonMap` (Activity + Plan projections share it); note for P4: `updated_at` is omitted-when-null (`updated_at?: string`), badge renderer must tolerate null/unknown `status` |
| P4 | Console plan checklist + `api.getTaskPlan` fetch hook | Not started | depends on P1..P3 |
| P5 | Planning E2E + Playwright scenario | Not started | depends on P4 |

## Notes

- This track is independent of the Supervisor track and carries **no open blockers** — it can ship first.
- Shared-file coordination (`state.py`, `_get_tools`, `ActivityPane.tsx`, `types/index.ts`) with the Supervisor track: worktree-isolate if both run concurrently (plan §A3).
- E9 (plan-injection KV-cache position) is resolved in this track's P2 (inject in the uncached suffix; assert position, not just unchanged-plan byte-identity).
