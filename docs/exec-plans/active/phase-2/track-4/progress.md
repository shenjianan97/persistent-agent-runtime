# Phase 2 Track 4 — Custom Tool Runtime (BYOT): Progress

| Task | Component | Status | Description |
|------|-----------|--------|-------------|
| Task 1 | Database Schema | Pending | `tool_servers` registry table with auth, status, unique constraint, indexes |
| Task 2 | Tool Server API | Pending | CRUD controller, service, repository + discover endpoint |
| Task 3 | Agent Config Extension | Pending | `tool_servers` field in agent config, validation against registry |
| Task 4 | MCP Session Manager | Pending | MCP session manager: connect, call_tool, close with concurrent sessions |
| Task 5 | Executor Integration | Pending | GraphExecutor: tool server lookup, schema conversion, merged tool binding |
| Task 6 | Console — Tool Servers | Pending | Tool Servers list, detail, register dialog, discover UI |
| Task 7 | Console — Agent Config | Pending | Tool server multi-select in agent create/edit forms |
| Task 8 | Integration Tests | Pending | E2E tests for custom tool lifecycle |

## Notes

- Task 1 must be completed before any downstream tasks
- Tasks 2, 3, 4 can proceed in parallel after Task 1
- Task 5 depends on Tasks 3, 4 (needs agent config extension + session manager)
- Task 6 depends on Task 2 (API must exist before console can consume it)
- Task 7 depends on Tasks 2, 3 (needs tool server list API + agent config validation)
- Task 8 depends on all backend tasks (1-5)
