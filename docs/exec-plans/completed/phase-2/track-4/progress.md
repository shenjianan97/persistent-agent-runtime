# Phase 2 Track 4 — Custom Tool Runtime (BYOT): Progress

| Task | Component | Status | Description |
|------|-----------|--------|-------------|
| Task 1 | Database Schema | Done | `tool_servers` registry table with auth, status, unique constraint, indexes |
| Task 2 | Tool Server API | Done | CRUD controller, service, repository + discover endpoint |
| Task 3 | Agent Config Extension | Done | `tool_servers` field in agent config, validation against registry |
| Task 4 | MCP Session Manager | Done | MCP session manager: connect, call_tool, close with concurrent sessions |
| Task 5 | Executor Integration | Done | GraphExecutor: tool server lookup, schema conversion, merged tool binding |
| Task 6 | Console — Tool Servers | Done | Tool Servers list, detail, register dialog, discover UI |
| Task 7 | Console — Agent Config | Done | Tool server multi-select in agent create/edit forms |
| Task 8 | Integration Tests | Done | E2E tests for custom tool lifecycle |
| Task 9 | Task Follow-Up | Done | Follow-up on completed tasks: API endpoint, worker resume path, Console UI |

## Notes

- Tasks 1-8 completed successfully
- Full test suite passes: Java API tests, Python worker tests (240+), Console tests (45), Integration tests (10)
- One bug found and fixed during integration testing: `McpSessionManager.connect()` needed to check `BaseException` instead of `Exception` to catch `BaseExceptionGroup` from the MCP SDK
- Task 9 added post-completion: enables continuing completed tasks with follow-up prompts, reusing existing checkpoint history
