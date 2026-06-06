# Agent Modes — Planning Primitive — Progress

Status tracking for the Planning Primitive track ([plan.md](./plan.md)).
Sibling track: [Supervisor Topology](../supervisor-topology/progress.md).
Statuses: `Not started` · `In progress` · `Done` · `Blocked`.

| Task | Description | Status | Note |
|---|---|---|---|
| P1 | `RuntimeState.plan` (full-replace reducer) + `plan_write` tool + v1 caps | Done | 33 unit tests pass; state schema updated to 14 fields |
| P2 | Post-compaction plan injection as a neutral Markdown `SystemMessage` | Not started | depends on P1 |
| P3 | `GET /v1/tasks/{id}/plan` read-only projection | Not started | depends on P1 (item shape); Java-only; **added scope (P1 review, 2026-06-06):** add `plan_write` to `ValidationConstants.ALLOWED_TOOLS` + validation test — without it the tool is unreachable via agent config and P5 dead-ends |
| P4 | Console plan checklist + `api.getTaskPlan` fetch hook | Not started | depends on P1..P3 |
| P5 | Planning E2E + Playwright scenario | Not started | depends on P4 |

## Notes

- This track is independent of the Supervisor track and carries **no open blockers** — it can ship first.
- Shared-file coordination (`state.py`, `_get_tools`, `ActivityPane.tsx`, `types/index.ts`) with the Supervisor track: worktree-isolate if both run concurrently (plan §A3).
- E9 (plan-injection KV-cache position) is resolved in this track's P2 (inject in the uncached suffix; assert position, not just unchanged-plan byte-identity).
