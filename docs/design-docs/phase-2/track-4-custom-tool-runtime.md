# Track 4 Design — Custom Tool Runtime (BYOT)

## Context

Track 1 established Agent as a first-class control-plane resource with CRUD APIs and config snapshotting. Track 2 expanded the runtime state model with durable pause states (`waiting_for_approval`, `waiting_for_input`, `paused`) and the append-only `task_events` audit timeline. Track 3 added agent-aware fair scheduling with concurrency limits and two-level budget enforcement.

Built-in tools (`web_search`, `read_url`, `calculator`, plus the Track 2 `request_human_input` HITL tool and the dev-only `dev_sleep`) are implemented as in-process Python functions. The `GraphExecutor._get_tools()` method in `executor/graph.py` builds `StructuredTool` objects directly from these functions and binds them to the LLM via `llm.bind_tools()`. There is no MCP protocol involvement at runtime — tools execute as direct async function calls within the same process.

An MCP server implementation exists in `tools/server.py` supporting both stdio and streamable HTTP transports, but it is used exclusively for manual testing and integration test validation, not by the runtime execution path.

The MCP SDK (`mcp==1.26.0`) is already a project dependency. Both `mcp.client.stdio` and `mcp.client.streamable_http` client transports are exercised in existing integration tests (`test_mcp_stdio_integration.py`, `test_mcp_http_integration.py`), confirming the SDK is functional and well-understood.

The `worker_pool_id` field exists on tasks (default `"shared"`) and is used for claim routing in the poller's SQL queries, but `DefaultTaskRouter` in `executor/router.py` always returns the same `GraphExecutor` regardless of pool. Agent config includes an `allowed_tools` list, but it only references built-in tool names.

Track 4 extends the runtime so that agents can use tools provided by external MCP servers, not just the built-in set. This is the BYOT (Bring Your Own Tools) foundation.

**Note:** Credential hardening (Secrets Manager migration, unified secret resolver, `provider_credentials` / `tool_credentials` registries) was originally planned as part of this track but has been deliberately deferred to Phase 3+. See [Phase 3+ design-notes.md, Section 8](../phase-3-plus/design-notes.md).

## Goals

- Let operators register external MCP tool servers by HTTP URL
- Let agents reference custom tool servers alongside built-in tools
- Worker discovers and invokes custom tools via MCP protocol at task execution time
- Preserve existing built-in tool behavior unchanged (in-process, no MCP overhead)
- Support bearer token authentication for MCP servers that require it

## Non-Goals

Track 4 does not include:

- stdio transport — would require MCP server code to be present on the worker machine, adding code-upload or pre-installation complexity
- container orchestration (ECS, Docker Compose) for MCP servers — operators run their own servers
- Secrets Manager migration or credential hardening — deferred to Phase 3+
- BYOK (Bring Your Own Key) for LLM provider credentials — deferred to Phase 3+
- non-idempotent tool safeguards (checkpoint-before-call, dead-letter on re-execution) — Track 5
- per-tool credential injection or isolation — deferred until secrets track
- multi-tenant MCP server isolation or network policy enforcement
- MCP server health monitoring, auto-restart, or liveness probes
- OAuth2 authentication flows — MCP roadmap targets Q2 2026 for standardized OAuth2; Track 4 uses simpler bearer token auth
- MCP gateway or proxy patterns — enterprise concern beyond current scope
- tool-level rate limiting or per-tool timeout configuration in the Console

## Core Decisions

- **MCP servers are operator-managed, not platform-managed.** The platform stores connection config and discovers tools. Operators are responsible for running and maintaining their MCP servers.
- **HTTP-only transport.** Operators run MCP servers independently and register the URL. The worker connects via streamable HTTP using `mcp.client.streamable_http.streamable_http_client()`. This avoids code upload, subprocess management, and binary distribution concerns.
- **Simple auth model.** Two modes: `none` (local/trusted network) and `bearer_token` (API key or token in `Authorization: Bearer <token>` header). Auth is configured per server registration and injected via httpx client headers. OAuth2 deferred.
- **Tool namespace: `server_name__tool_name` (double underscore separator).** Custom tools are namespaced by their server's registered name to avoid collisions with built-in tools and across servers. Built-in tools keep their unqualified names (`web_search`, `calculator`, etc.). The double underscore separator is chosen because LLM provider tool-calling APIs (notably OpenAI) restrict tool names to `[a-zA-Z0-9_-]+` — forward slashes and other special characters are not allowed.
- **Agent config extended with `tool_servers`.** The `agent_config` JSON gains a `tool_servers` field — an array of registered server names. This is separate from `allowed_tools`, which continues to reference built-in tools.
- **Auto-discovery via `tools/list`.** When a task starts, the worker connects to each referenced tool server and calls `tools/list` to discover available tools. Tool schemas are converted to LangChain `StructuredTool` objects and merged with built-in tools before binding to the LLM.
- **Connection per task execution, not persistent.** The worker opens an MCP session at task start and closes it on completion, pause, or error. This avoids long-lived connection management and simplifies cleanup.
- **`GraphExecutor` extended, not replaced.** The single executor handles both built-in and custom tools by merging tool lists. No new executor class or router branching needed.
- **Tool invocation failures use existing retry/dead-letter semantics.** If an MCP server is unavailable or a tool call fails, the task follows the same error handling path as built-in tool failures.
- **Custom tool calls use `_await_or_cancel()`.** All MCP tool invocations go through the existing cancellation-aware await pattern, ensuring lease revocation and task cancellation are respected during custom tool calls.
- **Maximum 128 tools per agent across all sources.** To prevent LLM context bloat and degraded model behavior, the total tool count (built-in + all custom tools from all servers) is capped at 128. Task execution fails with a clear error if the limit is exceeded.
- **MCP response size limit: 1 MB per tool call.** Responses exceeding this limit are truncated and a warning is logged. This prevents a misbehaving MCP server from exhausting worker memory.

### Deviation from Phase 2 design.md Section 4

The Phase 2 overview `design.md` Section 4 describes a platform-managed BYOT architecture where "the platform deploys [customer MCP servers] as isolated ECS tasks within the platform's VPC." Track 4 deliberately simplifies this to operator-managed MCP servers registered by URL. The rationale:

- The system is primarily used in local development where container orchestration is unnecessary overhead
- Operator-managed servers are the common pattern across the industry (OpenAI Agents SDK, Amazon Bedrock agents)
- Platform-managed deployment can be added incrementally in a future track without changing the registration model or worker integration

Additionally, `design.md` describes `worker_pool_id` as "doubling as a tool runtime routing key." Track 4 uses `tool_servers` in agent config instead of `worker_pool_id` for tool routing. This is a better fit because tool server selection is an agent-level concern (which tools should this agent use?), not a worker-pool-level concern (which worker should run this task?). `worker_pool_id` remains available for future use in compute isolation scenarios.

## Data Model

### New table: `tool_servers`

Registry of external MCP tool servers. One row per registered server per tenant.

| Column | Type | Constraints / Meaning |
|--------|------|----------------------|
| `server_id` | `UUID` | PRIMARY KEY DEFAULT gen_random_uuid() |
| `tenant_id` | `TEXT` | NOT NULL |
| `name` | `TEXT` | NOT NULL, human-readable identifier (e.g. "jira-tools", "github-tools") |
| `url` | `TEXT` | NOT NULL, MCP server HTTP endpoint (e.g. `http://localhost:9000/mcp`) |
| `auth_type` | `TEXT` | NOT NULL DEFAULT 'none', CHECK IN ('none', 'bearer_token') |
| `auth_token` | `TEXT` | nullable, bearer token value; only set when `auth_type = 'bearer_token'` |
| `status` | `TEXT` | NOT NULL DEFAULT 'active', CHECK IN ('active', 'disabled') |
| `created_at` | `TIMESTAMPTZ` | NOT NULL DEFAULT NOW() |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL DEFAULT NOW() |

**Unique constraint:** `(tenant_id, name)` — server names must be unique within a tenant.

**Indexes:** `(tenant_id, status)` — efficient lookup of active servers for a tenant.

**Migration:** `infrastructure/database/migrations/0008_tool_servers.sql` (depends on Track 3's `0007_scheduler_and_budgets.sql` having run first)

### Agent config extension

The `agent_config` JSON stored in the `agents` table gains a new optional field:

```json
{
  "system_prompt": "You are a helpful support assistant.",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "temperature": 0.7,
  "allowed_tools": ["web_search", "read_url"],
  "tool_servers": ["jira-tools", "github-tools"]
}
```

- `allowed_tools` continues to reference built-in tool names only
- `tool_servers` is an array of registered server names (matched against `tool_servers.name` for the task's tenant)
- if `tool_servers` is absent or empty, the task uses only built-in tools (backward compatible)
- the agent config is still snapshotted at task submission time, so tool server references are frozen per task

### Auth token storage

In Track 4, `auth_token` is stored as plaintext in PostgreSQL. This is acceptable for local development and early production (consistent with how `provider_keys.api_key` works in Phase 1). When the deferred Secret Management Hardening work (Phase 3+) is implemented, `auth_token` will migrate to a `secret_ref` pointing to Secrets Manager.

## Tool Discovery and Invocation

### Session manager

MCP sessions must remain open for the entire duration of task execution (discovery through invocation). The nested `async with` pattern used in integration tests does not work here because the graph is built once and tool calls happen later during `ToolNode` dispatch. Instead, Track 4 introduces an `McpSessionManager` that explicitly manages session lifetimes:

```python
class McpSessionManager:
    """Manages MCP client sessions across graph execution lifetime."""

    async def connect(self, servers: list[ToolServerConfig]) -> list[StructuredTool]:
        """Open sessions to all servers concurrently, discover tools, return merged tool list."""
        # Uses asyncio.gather() for parallel discovery across servers
        ...

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> Any:
        """Invoke a tool on a specific server's session."""
        ...

    async def close(self) -> None:
        """Close all open sessions. Safe to call multiple times."""
        ...
```

The session manager is created before graph construction and closed in a `finally` block after execution:

```python
session_manager = McpSessionManager(pool)
try:
    custom_tools = await session_manager.connect(tool_server_configs)
    all_tools = builtin_tools + custom_tools
    # Build and execute graph with all_tools
    ...
finally:
    await session_manager.close()
```

Internally, the session manager holds references to the `httpx.AsyncClient`, transport streams, and `ClientSession` objects for each server. The `connect()` method uses `asyncio.gather()` to open all sessions concurrently, reducing discovery latency when multiple servers are referenced.

### Discovery flow

When `GraphExecutor.execute_task()` is called for a task whose agent config includes `tool_servers`:

1. Read the `tool_servers` array from the snapshotted agent config
2. For each server name, look up the `tool_servers` row by `(tenant_id, name)` where `status = 'active'`
3. Create an `McpSessionManager` and call `connect()` with all server configs — this opens sessions concurrently and calls `tools/list` on each:
   ```python
   # Per-server connection (inside McpSessionManager.connect):
   headers = {}
   if server.auth_type == "bearer_token" and server.auth_token:
       headers["Authorization"] = f"Bearer {server.auth_token}"

   http_client = httpx.AsyncClient(headers=headers)
   read, write, _ = await streamable_http_client(server.url, http_client=http_client).__aenter__()
   session = await ClientSession(read, write).__aenter__()
   await session.initialize()
   tools_result = await session.list_tools()
   ```
4. Convert each discovered MCP tool schema to a LangChain `StructuredTool`:
   - Name: `{server_name}__{tool_name}` (namespaced)
   - Description: from MCP tool schema
   - Input schema: convert MCP JSON Schema to a dynamic Pydantic model
   - Invocation: closure that calls `session_manager.call_tool(server_name, tool_name, arguments)`
5. Merge built-in tools (from `allowed_tools`) with custom tools into a single list
6. Bind all tools to the LLM via `llm.bind_tools(all_tools)`

### Invocation flow

When the LLM generates a tool call for a custom tool (e.g. `jira-tools__create_issue`):

1. The `ToolNode` dispatches to the corresponding `StructuredTool`
2. The tool's coroutine calls `session.call_tool("create_issue", arguments)` on the MCP session
3. The MCP server processes the request and returns the result
4. The result is returned to the LLM as a `ToolMessage`

### Session lifecycle

MCP sessions are opened at the start of task execution and held open for the duration:

- **Task completes:** sessions closed in the cleanup path
- **Task paused (HITL or budget):** sessions closed before releasing the lease; re-opened if the task resumes on any worker
- **Task fails/dead-letters:** sessions closed in the error path
- **Worker crash:** sessions are abandoned (MCP servers should handle client disconnection gracefully)

### Error handling

- **Server unreachable at discovery time:** task transitions to `dead_letter` with error code `tool_server_unavailable` and the server name in the error message
- **Server unreachable during tool call:** `ToolExecutionError` raised, follows existing tool error handling (returned to LLM as error, or dead-letter if unrecoverable)
- **Tool call timeout:** configurable per-server timeout (default 30 seconds); exceeded timeout raises `ToolTransportError`
- **Server disabled between task submission and execution:** the snapshotted config references the server name; if the server is disabled at execution time, treat as unreachable

### Schema conversion

MCP tool schemas are JSON Schema objects. Converting to LangChain `StructuredTool` requires:

1. Parse the MCP tool's `inputSchema` (JSON Schema)
2. Dynamically create a Pydantic model from the JSON Schema using `pydantic.create_model()`
3. Wrap the MCP `call_tool()` invocation in an async function that goes through `_await_or_cancel()` for cancellation support
4. Create `StructuredTool.from_function(coroutine=..., name=..., description=..., args_schema=...)`

This follows the same pattern used for built-in tools in `_get_tools()`, extended to handle arbitrary schemas from external servers.

**Supported JSON Schema features:** `type`, `properties`, `required`, `description`, `default`, `enum`, basic `items` for arrays, and nested `object` types. These cover the vast majority of MCP tool schemas in practice.

**Unsupported (rejected with clear error):** `$ref` / `$defs` (JSON Schema references), `allOf` / `anyOf` / `oneOf` combinators, recursive types, and `patternProperties`. If an MCP server exposes a tool with an unsupported schema, that tool is skipped with a warning log, and the remaining tools from that server are still available. The task does not fail — only the incompatible tool is excluded.

## API Design

Track 4 adds a new `tool-servers` API resource following the same patterns as the Agent CRUD API from Track 1.

### Tool server management

**`POST /v1/tool-servers`** — Register a tool server

Request:
```json
{
  "name": "jira-tools",
  "url": "http://localhost:9000/mcp",
  "auth_type": "none"
}
```

Or with authentication:
```json
{
  "name": "github-tools",
  "url": "http://mcp-server:8080/mcp",
  "auth_type": "bearer_token",
  "auth_token": "ghp_xxxxxxxxxxxx"
}
```

Response: `201 Created`
```json
{
  "server_id": "a1b2c3d4-...",
  "tenant_id": "default",
  "name": "jira-tools",
  "url": "http://localhost:9000/mcp",
  "auth_type": "none",
  "status": "active",
  "created_at": "2026-04-07T10:00:00Z",
  "updated_at": "2026-04-07T10:00:00Z"
}
```

Validation:
- `name` must be unique within the tenant
- `name` must match `[a-z0-9][a-z0-9-]*` (lowercase alphanumeric + hyphens, used in tool namespacing)
- `url` must be a valid HTTP/HTTPS URL
- `auth_token` required when `auth_type = 'bearer_token'`

**`GET /v1/tool-servers`** — List registered servers

Query parameters: `status` (optional filter)

Response: array of tool server summaries (same shape as create response, `auth_token` never included in list responses).

**`GET /v1/tool-servers/{server_id}`** — Server detail

Response: tool server detail. `auth_token` is masked (e.g. `"ghp_xxxx...xxxx"`) — never returned in full.

**`PUT /v1/tool-servers/{server_id}`** — Update server config

Accepts same fields as create. Partial updates supported.

**`DELETE /v1/tool-servers/{server_id}`** — Remove server

Returns `204 No Content`. Hard delete — the server row is removed. Tasks already submitted with this server in their snapshotted config will fail at execution time with `tool_server_unavailable`. Agents that reference the deleted server in their `tool_servers` config are not automatically updated — the next task submission for those agents will fail validation (see Agent API changes below).

**`POST /v1/tool-servers/{server_id}/discover`** — Test connection and discover tools

Connects to the server, calls `tools/list`, and returns the discovered tools without executing any. Used for validation and UI display.

Response: `200 OK`
```json
{
  "server_id": "a1b2c3d4-...",
  "server_name": "jira-tools",
  "status": "reachable",
  "tools": [
    {
      "name": "create_issue",
      "description": "Create a new Jira issue",
      "input_schema": { "type": "object", "properties": { "project": { "type": "string" }, "summary": { "type": "string" } } }
    },
    {
      "name": "search_issues",
      "description": "Search Jira issues with JQL",
      "input_schema": { "type": "object", "properties": { "jql": { "type": "string" } } }
    }
  ]
}
```

If unreachable:
```json
{
  "server_id": "a1b2c3d4-...",
  "server_name": "jira-tools",
  "status": "unreachable",
  "error": "Connection refused: http://localhost:9000/mcp",
  "tools": []
}
```

### Agent API changes

The existing Agent CRUD API (`POST /v1/agents`, `PUT /v1/agents/{agent_id}`) accepts `tool_servers` in the `agent_config` JSON. Validation:
- Each name in `tool_servers` must reference an existing, active `tool_servers` row for the tenant
- Duplicate names are rejected

### Task API changes

No new task endpoints. The task detail response now includes `tool_servers` from the snapshotted agent config, visible alongside `allowed_tools`.

## Console Design

Track 4 extends the Console with a new Tool Servers management area and updates the Agent configuration UI.

### Tool Servers area

**Sidebar navigation:** Add "Tool Servers" entry below "Agents" in the sidebar.

**List page (`/tool-servers`):**
- Table with columns: Name, URL, Auth Type, Status, Created
- Status badge: `active` (green), `disabled` (gray)
- "Register Tool Server" button → opens register dialog
- Row click → navigates to detail page

**Detail page (`/tool-servers/:serverId`):**
- Server info: name, URL, auth type (token masked), status
- "Discover Tools" button → calls `/discover` endpoint, shows discovered tools in a table (name, description)
- Edit button → update URL, auth config, status
- Delete button with confirmation

**Register dialog:**
- Fields: Name, URL, Auth Type (dropdown: None / Bearer Token), Auth Token (shown when Bearer Token selected)
- On submit, calls `POST /v1/tool-servers`
- Optionally auto-discovers tools after registration

### Agent config editor

The existing agent create/edit form (`CreateAgentDialog` and `AgentDetailPage`) currently has sections: Agent Name, Model, System Prompt, Temperature, Tools (checkboxes for built-in tools), Human-in-the-Loop, and Scheduling & Budget.

Track 4 adds a **"Tool Servers"** section between the Tools checkboxes and the Human-in-the-Loop toggle:

- **Multi-select control** listing all active `tool_servers` for the tenant (fetched via `GET /v1/tool-servers?status=active`)
- Each option shows the server name and URL for clarity (e.g. "jira-tools — http://localhost:9000/mcp")
- Selected servers are saved to `agent_config.tool_servers`
- Empty selection is valid (agent uses only built-in tools)
- If no tool servers are registered for the tenant, the section shows a hint: "No tool servers registered. Register one in Tool Servers to give this agent custom tools."
- Both the `CreateAgentDialog` and the `AgentDetailPage` edit form must be updated

### Task detail

Task detail view shows the snapshotted `tool_servers` list when present, alongside the existing `allowed_tools` display.

## Observability and Events

### Task events

No new `task_events` event types are needed. Custom tool invocations are part of the normal task execution flow. However, when a task fails due to a tool server being unreachable, the `task_dead_lettered` event should include tool server context in its `details` JSON:

```json
{
  "error_code": "tool_server_unavailable",
  "tool_server_name": "jira-tools",
  "tool_server_url": "http://localhost:9000/mcp",
  "message": "Failed to connect to MCP server"
}
```

### Structured logging

The worker should emit structured log entries for MCP session lifecycle events:

- `mcp_session_opened` — server name, URL, tool count discovered
- `mcp_tool_invoked` — server name, tool name, duration_ms, success/failure
- `mcp_session_closed` — server name, reason (completed, paused, error)
- `mcp_session_error` — server name, error category, message

Tool server names and URLs may be logged. Auth tokens must never appear in logs.

## Risks and Open Questions

| Risk | Mitigation |
|------|-----------|
| MCP server unavailable at task execution time | Task dead-letters with clear error; existing retry semantics apply; discover endpoint lets operators validate before assigning to agents |
| Slow MCP tool calls block task execution | Per-call timeout (default 30s); `_await_or_cancel()` pattern ensures cancellation is respected |
| Tool schema changes between registration and execution | Tools are re-discovered on every task start via `tools/list`; no cached schema staleness |
| Tool name collisions across servers | Namespace with `server_name__tool_name`; built-in tools keep unqualified names |
| MCP session leak on worker crash | Sessions are TCP connections; server-side timeout handles abandoned clients |
| Auth token stored as plaintext in DB | Consistent with Phase 1 `provider_keys.api_key` pattern; migrates to Secrets Manager in Phase 3+ |
| Large number of tools from a single server could bloat LLM context | Document recommended limits; future work could add per-server tool allowlists |
| Agent config snapshot freezes tool server references | By design — execution stability; if a server is renamed, old tasks use the old name |
| Dynamic Pydantic model creation from arbitrary JSON Schema | Support common JSON Schema features; skip tools with unsupported schemas (`$ref`, combinators, recursion) with warning log |
| MCP server returns oversized response | 1 MB response size limit per tool call; truncate and warn |
| Tool count across all servers exceeds LLM practical limits | Hard cap of 128 tools per agent; fail task with clear error if exceeded |
| Agent references deleted tool server | Task submission validates `tool_servers` references; deletion does not auto-update agents but next submission fails validation |
| Tool name separator incompatible with LLM provider | Double underscore `__` separator chosen; compatible with OpenAI's `[a-zA-Z0-9_-]+` constraint |

**Open questions:**

1. Should the discover endpoint cache results in the `tool_servers` table (e.g. a `discovered_tools` JSONB column) for display in the Console without re-querying the server?
2. Should there be a configurable per-server tool call timeout, or is a global default sufficient for Track 4?
3. Should the agent config support per-server tool allowlists (e.g. `{"server": "jira-tools", "tools": ["create_issue"]}`) or should all discovered tools be available?

## Testing Strategy

### Unit tests

- MCP session management: open, discover, invoke, close lifecycle
- Tool schema conversion: MCP JSON Schema → Pydantic model → `StructuredTool`
- Tool namespacing: `server_name__tool_name` format, collision prevention with built-ins
- Auth header injection: `none` mode (no headers), `bearer_token` mode (Authorization header)
- Error handling: unreachable server, timeout, disabled server, invalid schema
- Agent config validation: `tool_servers` references checked against registered servers

### Database / migration verification

- Migration `0008` creates `tool_servers` table with correct schema and constraints
- Unique constraint on `(tenant_id, name)` prevents duplicate registrations
- `auth_type` CHECK constraint rejects invalid values

### Integration tests

- Register a tool server, create an agent referencing it, submit a task, verify the task discovers and invokes custom tools
- End-to-end: custom tool call result appears in task output
- Server unreachable at execution time → task dead-letters with `tool_server_unavailable`
- Disabled server → task fails gracefully
- Mixed tools: task uses both built-in (`web_search`) and custom tools in the same execution
- Bearer token auth: tool server requiring auth token works when token is correct, fails when missing/wrong
- Discover endpoint: returns tool list for reachable server, error for unreachable

### Console tests

- Tool server list page renders registered servers with correct status
- Register dialog creates a new server
- Detail page shows server info and discovered tools
- Agent config editor: tool server multi-select saves to `agent_config.tool_servers`
- Task detail shows tool server references from snapshotted config
