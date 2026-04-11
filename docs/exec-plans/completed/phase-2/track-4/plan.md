# Phase 2 Track 4 — Custom Tool Runtime (BYOT): Orchestrator Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let agents use tools provided by external MCP servers (Bring Your Own Tools), extending the runtime beyond built-in tools while preserving existing behavior unchanged.

**Architecture:** Operators register external MCP tool servers by HTTP URL. Agents reference registered servers in their config. At task execution time, the worker connects to referenced servers via streamable HTTP, discovers tools via `tools/list`, converts MCP tool schemas to LangChain `StructuredTool` objects, and merges them with built-in tools before binding to the LLM. Sessions are opened per task execution and closed on completion, pause, or error.

**Tech Stack:** PostgreSQL (tool server registry), Spring Boot (CRUD API + discover endpoint), Python asyncpg + MCP SDK (worker session management + tool invocation), React/TypeScript (console management UI)

---

## A1. Implementation Overview

Track 4 extends the Phase 1/2 runtime with:
1. Database schema for the `tool_servers` registry table
2. Tool Server CRUD API with discover endpoint (Spring Boot)
3. Agent config extension with `tool_servers` field and validation
4. MCP session manager for concurrent server connections and tool invocation (Python worker)
5. GraphExecutor integration: tool discovery, schema conversion, and merged tool binding
6. Console Tool Servers management area (list, detail, register, discover)
7. Console Agent config editor updates (tool server multi-select)
8. Integration tests for the full custom tool lifecycle

**Canonical design contract:** `docs/design-docs/phase-2/track-4-custom-tool-runtime.md`

---

## A2. Impacted Components

| Component | Path | Change Type | Description |
|-----------|------|-------------|-------------|
| Database Schema | `infrastructure/database/migrations/` | new migration | `0008_tool_servers.sql`: tool server registry table with auth, status, and indexes |
| Tool Server API | `services/api-service/` | new code | CRUD controller, service, repository for tool servers + discover endpoint |
| Agent API | `services/api-service/` | modification | `tool_servers` field in agent config validation, cross-reference check against registry |
| MCP Session Manager | `services/worker-service/executor/` | new code | `mcp_session.py`: session lifecycle, concurrent connect, tool invocation |
| Worker Executor | `services/worker-service/executor/graph.py` | modification | Tool discovery integration, schema conversion, merged tool binding in `execute_task()` |
| Console — Tool Servers | `services/console/src/features/tool-servers/` | new code | List page, detail page, register dialog, discover UI |
| Console — Agent Config | `services/console/src/features/agents/` | modification | Tool server multi-select in create/edit forms |
| Integration Tests | `tests/backend-integration/` | new code | Custom tool lifecycle E2E tests |

---

## A3. Dependency Graph

```
Task 1 (Schema) ─┬──→ Task 2 (Tool Server API) ──→ Task 6 (Console — Tool Servers) ──┐
                  │                                                                     │
                  ├──→ Task 3 (Agent Config Ext) ──→ Task 7 (Console — Agent Config) ──→├──→ Task 8 (Integration Tests)
                  │                                                                     │
                  ├──→ Task 4 (MCP Session Mgr) ──→ Task 5 (Executor Integration) ─────→│
                  │                                                                     │
                  └──→ Task 2 ──→ Task 7 (needs tool server list API)                   │
                       Task 3 ──→ Task 5 (needs tool_servers in agent config)            │
```

**Parallelization opportunities:**
- After Task 1: Tasks 2, 3, 4 can all start in parallel
- Task 5 depends on Tasks 3, 4 (needs agent config extension + session manager)
- Task 6 depends on Task 2 (API must exist before console can consume it)
- Task 7 depends on Tasks 2, 3 (needs tool server list API + agent config validation)
- Task 8 depends on all backend tasks (1-5)

---

## A4. Data / API / Schema Changes

**New table (`tool_servers`):** Additive. No existing data affected.

**Agent config extension:** Backward compatible — new optional `tool_servers` array field. Absent or empty means built-in tools only.

**Tool Server API:** New resource at `/v1/tool-servers` with full CRUD + discover endpoint.

**Agent API:** Backward compatible — `tool_servers` in agent config validated against registry on create/update. Existing agents without `tool_servers` are unaffected.

**Task API:** No new endpoints. Task detail response includes `tool_servers` from snapshotted agent config when present.

---

## A4.1. Task Handoff Outputs

| Task | Output |
|------|--------|
| Task 1 | Migration `0008_tool_servers.sql` with registry table, unique constraint, indexes |
| Task 2 | `ToolServerController`, `ToolServerService`, `ToolServerRepository` + discover endpoint with MCP probe |
| Task 3 | `AgentConfigRequest.toolServers` field, `ConfigValidationHelper.validateToolServers()`, cross-reference check |
| Task 4 | `McpSessionManager` class: `connect()`, `call_tool()`, `close()` with concurrent session management |
| Task 5 | `execute_task()` extended: tool server lookup, session manager lifecycle, schema conversion, merged tool binding |
| Task 6 | Console: `ToolServersListPage`, `ToolServerDetailPage`, `RegisterToolServerDialog`, discover UI |
| Task 7 | Console: tool server multi-select in `CreateAgentDialog` and `AgentDetailPage` edit form |
| Task 8 | Integration tests: register server, create agent, submit task, verify custom tool discovery and invocation |

---

## A5. Integration Points

| Caller | Callee | Interface Change | Failure Handling |
|--------|--------|-------------------|-----------------|
| Tool Server API | PostgreSQL `tool_servers` | New CRUD queries on `tool_servers` table | Unique constraint violation → 409 Conflict; not found → 404 |
| Tool Server API (discover) | External MCP Server | HTTP connection + `tools/list` call | Unreachable → return `status: "unreachable"` with error message |
| Agent API (create/update) | PostgreSQL `tool_servers` | Validate `tool_servers` names exist and are active | Unknown/disabled server → 400 validation error |
| Worker Executor | PostgreSQL `tool_servers` | Lookup server configs by `(tenant_id, name)` at task start | Missing/disabled server → `dead_letter` with `tool_server_unavailable` |
| Worker Executor | External MCP Server | Streamable HTTP session + `tools/list` + `call_tool` | Unreachable → `dead_letter`; tool call timeout → `ToolTransportError` |
| Console | API `/v1/tool-servers` | New CRUD + discover endpoints | Error toast on failure |
| Console | API `/v1/agents` | Extended `agent_config` with `tool_servers` | Backward compatible rendering |

---

## A6. Deployment and Rollout

Same pattern as Tracks 1-3: single coordinated deployment. Migration `0008` is picked up by the schema-bootstrap ledger.

**Deployment order:** Database migration MUST run before new API/worker code deploys. The tool server API references `tool_servers` table which must exist.

**For local development:** `make db-reset` applies all migrations including `0008`.

---

## A7. Observability

- `tool_server_unavailable` error code in `task_dead_lettered` events includes server name and URL in `details` JSONB
- Structured logging on MCP session lifecycle: `mcp_session_opened`, `mcp_tool_invoked`, `mcp_session_closed`, `mcp_session_error`
- Auth tokens must never appear in logs
- Discover endpoint provides pre-flight validation for operators

---

## A8. Risks and Open Questions

| Risk | Mitigation |
|------|-----------|
| MCP server unavailable at task execution time | Task dead-letters with clear error; discover endpoint validates before assignment |
| Slow MCP tool calls block task execution | Per-call timeout (default 30s); `_await_or_cancel()` ensures cancellation respected |
| Tool schema changes between registration and execution | Re-discovered on every task start via `tools/list` |
| Tool name collisions across servers | Namespace with `server_name__tool_name`; built-in tools keep unqualified names |
| Auth token stored as plaintext in DB | Consistent with Phase 1 `provider_keys.api_key`; migrates to Secrets Manager in Phase 3+ |
| Dynamic Pydantic model creation from arbitrary JSON Schema | Support common features; skip unsupported tools with warning log |
| MCP response exceeds memory limits | 1 MB response size limit per tool call; truncate and warn |
| Tool count exceeds LLM practical limits | Hard cap of 128 tools per agent; fail task with clear error |
| Agent references deleted tool server | Task submission validates references; deletion does not auto-update agents |

---

## A9. Orchestrator Guidance

- Use `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` as the canonical design contract
- Task 1 must land first. Tasks 2, 3, 4 can proceed in parallel after Task 1
- The existing Agent CRUD pattern (controller → service → repository) is the direct template for Tool Server CRUD
- Tool namespace separator is double underscore `__` (e.g., `jira-tools__create_issue`)
- Server names must match `[a-z0-9][a-z0-9-]*` for safe namespacing
- `auth_token` is stored as plaintext (consistent with `provider_keys.api_key`); never returned in full via API
- MCP sessions are connection-per-task, not persistent — opened at task start, closed on completion/pause/error
- The `_await_or_cancel()` pattern in `GraphExecutor` must wrap all MCP tool invocations
- Schema conversion supports common JSON Schema features; tools with unsupported schemas (`$ref`, combinators, recursion) are skipped with a warning
- Maximum 128 tools per agent (built-in + custom); 1 MB response size limit per tool call
- `tool_servers` in agent config is separate from `allowed_tools` — both are snapshotted at task submission
- HTTP-only transport — no stdio support in Track 4
- Do not add per-tool credential injection, rate limiting, or timeout configuration — deferred to future tracks
- Do not add OAuth2 authentication — bearer token only for Track 4

---

## A10. Key Design Decisions

1. **Operator-managed, not platform-managed.** The platform stores connection config and discovers tools. Operators run and maintain their own MCP servers.

2. **HTTP-only transport.** Workers connect via `mcp.client.streamable_http.streamable_http_client()`. Avoids code upload, subprocess management, and binary distribution.

3. **Simple auth model.** Two modes: `none` and `bearer_token`. Auth configured per server registration, injected via httpx client headers.

4. **Tool namespace: `server_name__tool_name`.** Double underscore separator compatible with LLM provider tool-name constraints (`[a-zA-Z0-9_-]+`).

5. **Connection per task execution.** Session opened at task start, closed on completion/pause/error. Avoids long-lived connection management.

6. **GraphExecutor extended, not replaced.** Single executor handles both built-in and custom tools by merging tool lists.

7. **Auto-discovery via `tools/list`.** Tools re-discovered on every task start — no cached schema staleness.

8. **Tool invocation failures use existing retry/dead-letter semantics.** No new error handling paths needed.

---

## B. Agent Task Files

| Task | File | Description |
|------|------|-------------|
| Task 1 | [task-1-database-migration.md](agent_tasks/task-1-database-migration.md) | Schema: `tool_servers` registry table with auth, status, unique constraint, indexes |
| Task 2 | [task-2-tool-server-api.md](agent_tasks/task-2-tool-server-api.md) | Tool Server CRUD controller, service, repository + discover endpoint |
| Task 3 | [task-3-agent-config-extension.md](agent_tasks/task-3-agent-config-extension.md) | `tool_servers` field in agent config, validation against registry |
| Task 4 | [task-4-mcp-session-manager.md](agent_tasks/task-4-mcp-session-manager.md) | MCP session manager: connect, call_tool, close with concurrent sessions |
| Task 5 | [task-5-executor-integration.md](agent_tasks/task-5-executor-integration.md) | GraphExecutor integration: tool server lookup, schema conversion, merged binding |
| Task 6 | [task-6-console-tool-servers.md](agent_tasks/task-6-console-tool-servers.md) | Console: Tool Servers list, detail, register dialog, discover UI |
| Task 7 | [task-7-console-agent-config.md](agent_tasks/task-7-console-agent-config.md) | Console: tool server multi-select in agent create/edit forms |
| Task 8 | [task-8-integration-tests.md](agent_tasks/task-8-integration-tests.md) | E2E tests for custom tool lifecycle |
| Task 9 | [task-9-follow-up.md](agent_tasks/task-9-follow-up.md) | Follow-up on completed tasks: API endpoint, worker resume path, Console UI |
