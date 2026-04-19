# Execution Plans

Implementation plans organized by delivery phase.

- `active/` — Plans currently being executed
- `completed/` — Archived completed plans
- [tech-debt-tracker.md](./tech-debt-tracker.md) — Known tech debt items

For the full inventory of phases, tracks, and their status, see [STATUS.md](../../STATUS.md).

## Console tasks

Any `agent_tasks/task-*.md` whose scope touches `services/console/` must embed the checklist from [docs/CONSOLE_TASK_CHECKLIST.md](../CONSOLE_TASK_CHECKLIST.md) under its Acceptance Criteria section. The checklist ensures browser-verification scenarios, `data-testid`s, and the [Agent-Config Coverage Matrix](../CONSOLE_BROWSER_TESTING.md#agent-config-coverage-matrix) are kept in sync with the UI — enforcing the blocking rule in [AGENTS.md §Browser Verification (Console Changes)](../../AGENTS.md#browser-verification-console-changes--blocking).
