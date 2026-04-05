# Phase 2 Track 3 — Scheduler and Budgets: Progress

| Task | Component | Status | Description |
|------|-----------|--------|-------------|
| Task 1 | Database Schema | Not Started | Agent budget columns, task pause columns, scheduler state tables, indexes |
| Task 2 | Incremental Cost | Not Started | Per-checkpoint cost writes to `agent_cost_ledger` |
| Task 3 | Scheduler Claim | Not Started | Agent-aware round-robin claim query |
| Task 4 | Budget Enforcement | Not Started | Budget check + pause at checkpoint boundaries |
| Task 5 | Reaper Recovery | Not Started | Hourly auto-recovery, running-count reconciliation |
| Task 6 | API Extensions | Not Started | Agent budget fields, task pause fields, resume endpoint |
| Task 7 | Console Updates | Not Started | Agent budget form, task pause rendering, resume action |
| Task 8 | Integration Tests | Not Started | E2E tests for scheduler, budgets, pause/resume |

## Notes

- Task 1 must be completed before any downstream tasks
- Tasks 2, 3, 5, 6 can proceed in parallel after Task 1
- Task 4 depends on Task 2 (incremental cost must exist before budget enforcement)
- Task 7 depends on Task 6 (API must expose fields before console can consume them)
- Task 8 depends on all backend tasks (1-6)
