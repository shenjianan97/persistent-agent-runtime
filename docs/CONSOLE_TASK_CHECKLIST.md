# Console Task Checklist

Every `agent_tasks/task-*.md` spec whose scope touches the Console (`services/console/`) **must** embed this checklist verbatim under its **Acceptance Criteria** section. The canonical rules, coverage matrix, scenario templates, and selection matrix live in [docs/CONSOLE_BROWSER_TESTING.md](./CONSOLE_BROWSER_TESTING.md) — this file is the per-task merge gate.

## Checklist (copy into your task spec)

- [ ] Named the scenario template(s) — A / B / C / D or a combination — that the new work uses. See [§Scenario Templates](./CONSOLE_BROWSER_TESTING.md#scenario-templates).
- [ ] Listed the [§Agent-Config Coverage Matrix](./CONSOLE_BROWSER_TESTING.md#agent-config-coverage-matrix) cells the change touches (if any) and the scenario numbers they will cite after merge. A `⚠ gap` anywhere in an affected row requires closing the gap **in this same task** (not deferring to tech-debt).
- [ ] Every new interactive element has a stable `data-testid`.
- [ ] Added a new scenario OR extended an existing one with assertions **at the field + `data-testid` level** — not a bullet that merely says "the section renders." The scenario diff is visible in the merge commit.
- [ ] Same commit updates the [§When to Run Which Scenarios](./CONSOLE_BROWSER_TESTING.md#when-to-run-which-scenarios) selection matrix.
- [ ] If the feature code renders the sub-object on >1 surface, Template D's four parity assertions appear in the scenario (even if only one coverage-matrix cell is cited).
- [ ] The merge commit or PR description names the scenario numbers that were run in the browser, and the orchestrator (not the implementer subagent) runs them — see [AGENTS.md §Parallel Subagent Safety](../AGENTS.md#parallel-subagent-safety). A subagent implementing this task must not tick this box itself.

## When this checklist applies

- Any change under `services/console/src/`.
- Any change to an API contract that surfaces on the Console — the Console-side follow-up needs the checklist.
- Removing or renaming a `data-testid` (silently breaks scenarios; update the scenario first).

## When this checklist does NOT apply

- Backend-only changes (`services/api-service/`, `services/worker-service/` without a new Console-visible field).
- Documentation-only changes.
- Pure refactors with zero behavior change — note `no behavior change` in the commit message.

## Worked example — adding a new config sub-object to 3 surfaces

Task: "add `guardrails` sub-object with fields on Create dialog, Edit form, Submit page (read-only)."

1. **Template choice:** D (rendering on >1 surface).
2. **Matrix cells:** new row `guardrails`; Create = `2`, Agent Detail = `2`, Edit = `2`, Submit = `3`, Task Detail = `—`.
3. **Scenario edits:** extend Scenario 2 to assert the Create-dialog + Edit-form sub-section with each field name + `data-testid`; extend Scenario 3 for the Submit-side read-only block. Both extensions include Template D's four parity assertions.
4. **Selection matrix:** add row `Agent guardrails feature → 1, 2, 3`.
5. **Hand-off:** ship code + unit tests + the two scenario diffs + the matrix row; leave the orchestrator to run Playwright and record scenario numbers on the merge commit.
