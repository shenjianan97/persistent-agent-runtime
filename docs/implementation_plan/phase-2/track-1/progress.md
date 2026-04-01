# Phase 2 Track 1 — Agent Control Plane: Progress

| Task | Component | Status | Description |
|------|-----------|--------|-------------|
| Task 1 | Database Schema | Not Started | Agents table, tasks.agent_display_name_snapshot, FK constraint, seed data |
| Task 2 | Agent CRUD API | Not Started | AgentController/Service/Repository, POST/GET/GET/{id}/PUT at /v1/agents |
| Task 3 | Task Submission Refactor | Not Started | Remove inline agent_config, resolve from agents table, snapshot config + display_name |
| Task 4 | Task Response Enrichment | Not Started | Add agent_display_name to all task-facing responses |
| Task 5 | Console: Agents Area | Not Started | /agents list + /agents/:agentId detail, sidebar nav, create dialog |
| Task 6 | Console: Submit + Task Views | Not Started | Agent selector submit page, display_name in task list/detail/dead-letter |
| Task 7 | Integration Tests + Worker | Not Started | Updated test contract, agent CRUD tests, worker FK compat |

## Notes

- Task 1 must be completed before any downstream tasks
- Tasks 2 and 4 can proceed in parallel after Task 1
- Task 3 depends on both Task 1 and Task 2
- Task 5 depends on Task 2
- Task 6 depends on Tasks 3, 4, and 5
- Task 7 depends on Tasks 1-4
