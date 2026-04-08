<!-- AGENT_TASK_START: task-5-executor-integration.md -->

# Task 5 — GraphExecutor Integration: Tool Discovery, Schema Conversion, and Merged Binding

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` — canonical design contract (Tool Discovery and Invocation, Schema Conversion sections)
2. `services/worker-service/executor/graph.py` — current `_get_tools()`, `_build_graph()`, and `execute_task()` implementation
3. `services/worker-service/executor/mcp_session.py` — Task 4 output: `McpSessionManager`, `ToolServerConfig`, `McpConnectionError`, `McpToolCallError`
4. `services/worker-service/tools/definitions.py` — built-in tool definitions and `ToolDependencies`
5. `infrastructure/database/migrations/0008_tool_servers.sql` — `tool_servers` table schema

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-4/progress.md` to "Done".

## Context

The `GraphExecutor` currently builds tools exclusively from built-in Python functions via `_get_tools()`. Track 4 extends this to also discover and bind tools from external MCP servers.

When a task's agent config includes `tool_servers`, the executor:
1. Looks up server configs from the `tool_servers` DB table
2. Creates an `McpSessionManager` and connects to all referenced servers
3. Converts discovered MCP tool schemas to LangChain `StructuredTool` objects
4. Merges custom tools with built-in tools
5. Binds all tools to the LLM

The session manager is created before graph construction and closed in a `finally` block after execution.

## Task-Specific Shared Contract

- Tool namespace: `{server_name}__{tool_name}` (double underscore separator)
- Built-in tools keep unqualified names (`web_search`, `calculator`, etc.)
- Maximum 128 tools per agent across all sources
- Schema conversion supports: `type`, `properties`, `required`, `description`, `default`, `enum`, basic `items` for arrays, nested `object` types
- Unsupported schemas (`$ref`, `allOf`/`anyOf`/`oneOf`, recursive types, `patternProperties`) cause the tool to be skipped with a warning
- All MCP tool invocations go through `_await_or_cancel()` for cancellation support
- Server unreachable at discovery time → task dead-letters with error code `tool_server_unavailable`
- Disabled server at execution time → treated as unreachable

## Affected Component

- **Service/Module:** Worker Service — Executor
- **File paths:**
  - `services/worker-service/executor/graph.py` (modify — extend `_build_graph()` and `execute_task()`)
  - `services/worker-service/executor/schema_converter.py` (new — MCP JSON Schema → Pydantic model conversion)
  - `services/worker-service/tests/test_schema_converter.py` (new — schema conversion unit tests)
  - `services/worker-service/tests/test_executor.py` (modify — add tests for custom tool integration)
- **Change type:** modification + new code

## Dependencies

- **Must complete first:** Task 3 (Agent Config Extension — `tool_servers` field in agent config), Task 4 (MCP Session Manager — `McpSessionManager` class)
- **Provides output to:** Task 8 (Integration Tests)
- **Shared interfaces/contracts:** `McpSessionManager` API, `ToolServerConfig` dataclass, `agent_config.tool_servers` field

## Implementation Specification

### Step 1: Create schema converter module

Create `services/worker-service/executor/schema_converter.py`:

```python
"""Convert MCP tool JSON schemas to LangChain StructuredTool objects."""

import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)

# JSON Schema types that are not supported and cause the tool to be skipped
UNSUPPORTED_SCHEMA_KEYWORDS = {"$ref", "$defs", "allOf", "anyOf", "oneOf", "patternProperties"}

MAX_TOOLS_PER_AGENT = 128


def json_schema_to_pydantic(schema: dict[str, Any], model_name: str = "DynamicModel") -> type[BaseModel] | None:
    """Convert a JSON Schema object to a Pydantic model class.

    Returns None if the schema contains unsupported features.
    """
    if not schema or schema.get("type") != "object":
        # Empty or non-object schema → accept any dict
        return create_model(model_name)

    # Check for unsupported keywords
    for key in UNSUPPORTED_SCHEMA_KEYWORDS:
        if key in schema:
            logger.warning("Unsupported JSON Schema keyword '%s' in schema for %s", key, model_name)
            return None
        # Check nested properties too
        for prop_schema in schema.get("properties", {}).values():
            if isinstance(prop_schema, dict) and key in prop_schema:
                logger.warning("Unsupported JSON Schema keyword '%s' in nested property of %s", key, model_name)
                return None

    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))

    field_definitions: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        python_type = _json_type_to_python(prop_schema, f"{model_name}_{prop_name}")
        if python_type is None:
            logger.warning("Skipping unsupported property '%s' in %s", prop_name, model_name)
            continue

        description = prop_schema.get("description", "")
        default = prop_schema.get("default", ...)

        if prop_name in required_fields:
            field_definitions[prop_name] = (
                python_type,
                Field(description=description) if description else Field(...),
            )
        else:
            field_definitions[prop_name] = (
                python_type | None,
                Field(default=default if default is not ... else None, description=description),
            )

    return create_model(model_name, **field_definitions)


def _json_type_to_python(schema: dict[str, Any], context: str) -> type | None:
    """Map a JSON Schema type to a Python type."""
    if not isinstance(schema, dict):
        return Any

    # Check for unsupported keywords in this schema node
    for key in UNSUPPORTED_SCHEMA_KEYWORDS:
        if key in schema:
            return None

    json_type = schema.get("type", "string")
    enum_values = schema.get("enum")

    if enum_values is not None:
        # For enums, use str (the enum values are informational to the LLM via description)
        return str

    if json_type == "string":
        return str
    elif json_type == "integer":
        return int
    elif json_type == "number":
        return float
    elif json_type == "boolean":
        return bool
    elif json_type == "array":
        items_schema = schema.get("items", {})
        item_type = _json_type_to_python(items_schema, f"{context}_item")
        if item_type is None:
            return None
        return list[item_type]
    elif json_type == "object":
        # Nested object — create a sub-model
        nested = json_schema_to_pydantic(schema, context)
        return nested if nested is not None else dict
    else:
        return str  # Default to string for unknown types


def mcp_tools_to_structured_tools(
    server_name: str,
    tool_schemas: list[dict[str, Any]],
    call_fn,
    cancel_event=None,
    await_or_cancel_fn=None,
    task_id: str = "",
) -> list[StructuredTool]:
    """Convert MCP tool schemas to LangChain StructuredTool objects.

    Args:
        server_name: Name of the MCP server (used for namespacing)
        tool_schemas: List of tool dicts from McpSessionManager.connect()
        call_fn: async callable(server_name, tool_name, arguments) -> result
        cancel_event: asyncio.Event for cancellation (optional)
        await_or_cancel_fn: async callable for cancellation-aware awaiting (optional)
        task_id: task ID for logging

    Returns:
        List of StructuredTool objects
    """
    tools = []
    for schema in tool_schemas:
        tool_name = schema.get("name", "")
        description = schema.get("description", "")
        input_schema = schema.get("inputSchema", {})

        namespaced_name = f"{server_name}__{tool_name}"

        # Convert JSON Schema to Pydantic model
        pydantic_model = json_schema_to_pydantic(input_schema, namespaced_name)
        if pydantic_model is None:
            logger.warning(
                "Skipping tool %s: unsupported JSON Schema features",
                namespaced_name,
            )
            continue

        # Create async invocation function
        # Capture loop variables in closure defaults
        async def invoke_tool(
            _server=server_name,
            _tool=tool_name,
            _namespaced=namespaced_name,
            **kwargs,
        ):
            coro = call_fn(_server, _tool, kwargs)
            if await_or_cancel_fn and cancel_event:
                return await await_or_cancel_fn(
                    coro, cancel_event, task_id=task_id, operation=_namespaced
                )
            return await coro

        tool = StructuredTool.from_function(
            coroutine=invoke_tool,
            name=namespaced_name,
            description=description,
            args_schema=pydantic_model,
        )
        tools.append(tool)

    return tools
```

### Step 2: Extend execute_task() to look up tool server configs

Modify `services/worker-service/executor/graph.py` — add a method to look up tool server configs from the database:

```python
from executor.mcp_session import McpSessionManager, ToolServerConfig, McpConnectionError

async def _lookup_tool_server_configs(
    self, conn, tenant_id: str, server_names: list[str]
) -> list[ToolServerConfig]:
    """Look up tool server configs from the database.

    Args:
        conn: asyncpg connection
        tenant_id: tenant ID
        server_names: list of server names from agent config

    Returns:
        List of ToolServerConfig objects

    Raises:
        McpConnectionError: if any server is not found or disabled
    """
    if not server_names:
        return []

    rows = await conn.fetch(
        """
        SELECT name, url, auth_type, auth_token, status
        FROM tool_servers
        WHERE tenant_id = $1 AND name = ANY($2)
        """,
        tenant_id,
        server_names,
    )

    found = {row["name"]: row for row in rows}

    configs = []
    for name in server_names:
        row = found.get(name)
        if row is None:
            raise McpConnectionError(
                server_name=name,
                server_url="unknown",
                message=f"Tool server '{name}' not found in registry",
            )
        if row["status"] != "active":
            raise McpConnectionError(
                server_name=name,
                server_url=row["url"],
                message=f"Tool server '{name}' is disabled",
            )
        configs.append(
            ToolServerConfig(
                name=row["name"],
                url=row["url"],
                auth_type=row["auth_type"],
                auth_token=row["auth_token"],
            )
        )

    return configs
```

### Step 3: Extend _build_graph() to accept custom tools

Modify `_build_graph()` to accept an optional list of additional tools:

```python
async def _build_graph(
    self,
    agent_config: dict[str, Any],
    *,
    cancel_event: asyncio.Event,
    task_id: str,
    custom_tools: list[StructuredTool] | None = None,
) -> StateGraph:
    """Assembles the LangGraph state machine and binds tools."""
    # ... existing LLM initialization ...

    # Register built-in tools
    tools = self._get_tools(allowed_tools, cancel_event=cancel_event, task_id=task_id)

    # Merge custom tools from MCP servers
    if custom_tools:
        tools = tools + custom_tools

    # Enforce tool count limit
    if len(tools) > MAX_TOOLS_PER_AGENT:
        raise ValueError(
            f"Agent has {len(tools)} tools (max {MAX_TOOLS_PER_AGENT}). "
            f"Reduce the number of tool servers or use servers with fewer tools."
        )

    # ... rest of existing _build_graph() (bind_tools, graph construction) ...
```

Import `MAX_TOOLS_PER_AGENT` from `executor.schema_converter`.

### Step 4: Integrate MCP session lifecycle into execute_task()

Modify `execute_task()` to add MCP session management around graph execution. **Critical:** The existing `_handle_dead_letter()` method must be used for dead-lettering (not a new helper). Its signature is:

```python
async def _handle_dead_letter(self, task_id: str, tenant_id: str, agent_id: str,
                               reason: str, error_msg: str, error_code: str | None = None)
```

This method handles lease validation (`lease_owner`), `running_task_count` decrement, and task event insertion. Do NOT create a separate `_dead_letter_task()` method.

Add to `execute_task()`:

```python
async def execute_task(self, task_data: dict[str, Any], cancel_event: asyncio.Event) -> None:
    task_id = str(task_data["task_id"])
    tenant_id = task_data["tenant_id"]
    agent_id = task_data.get("agent_id") or "unknown"
    agent_config = json.loads(task_data["agent_config_snapshot"])
    # ... existing setup ...

    # Extract tool_servers from agent config
    tool_server_names = agent_config.get("tool_servers", [])

    session_manager = None
    custom_tools = []

    try:
        # Look up and connect to MCP tool servers if configured
        if tool_server_names:
            async with self.pool.acquire() as conn:
                server_configs = await self._lookup_tool_server_configs(
                    conn, tenant_id, tool_server_names
                )

            session_manager = McpSessionManager()
            try:
                tools_by_server = await session_manager.connect(server_configs)
            except McpConnectionError as e:
                logger.error(
                    "tool_server_unavailable",
                    extra={
                        "task_id": task_id,
                        "server_name": e.server_name,
                        "server_url": e.server_url,
                        "error": str(e),
                    },
                )
                # Dead-letter using the existing _handle_dead_letter method
                # which handles lease validation, running_task_count decrement,
                # and task event insertion
                await self._handle_dead_letter(
                    task_id,
                    tenant_id,
                    agent_id,
                    reason="tool_server_unavailable",
                    error_msg=str(e),
                    error_code="tool_server_unavailable",
                )
                return

            # Convert MCP tool schemas to StructuredTool objects
            from executor.schema_converter import mcp_tools_to_structured_tools

            for server_name, tool_schemas in tools_by_server.items():
                server_tools = mcp_tools_to_structured_tools(
                    server_name=server_name,
                    tool_schemas=tool_schemas,
                    call_fn=session_manager.call_tool,
                    cancel_event=cancel_event,
                    await_or_cancel_fn=self._await_or_cancel,
                    task_id=task_id,
                )
                custom_tools.extend(server_tools)

            logger.info(
                "custom_tools_discovered",
                extra={
                    "task_id": task_id,
                    "server_count": len(tools_by_server),
                    "tool_count": len(custom_tools),
                },
            )

        # Build graph with merged tools
        graph = await self._build_graph(
            agent_config,
            cancel_event=cancel_event,
            task_id=task_id,
            custom_tools=custom_tools if custom_tools else None,
        )

        # ... existing graph execution (compile, astream, cost tracking, etc.) ...

        # IMPORTANT: When the existing pause logic triggers (HITL or budget),
        # MCP sessions must be closed before releasing the lease.
        # The existing pause path should call session_manager.close("paused")
        # before returning. See Step 5 for details.

    finally:
        # Close MCP sessions in cleanup — covers completion, error, and
        # any path not handled by explicit close calls above.
        if session_manager is not None:
            await session_manager.close()
```

### Step 5: Handle MCP session closure on task pause

The design doc requires that MCP sessions are closed before releasing the lease when a task pauses (HITL or budget). The existing pause logic in `execute_task()` transitions the task to a paused state and returns.

Find the existing pause transition code in `execute_task()` — it will be in the budget enforcement section (Track 3) and possibly the HITL section (Track 2). Before each `return` statement in a pause path, add:

```python
# Close MCP sessions before releasing lease on pause
if session_manager is not None:
    await session_manager.close("paused")
    session_manager = None  # Prevent double-close in finally block
```

The `finally` block's `session_manager.close()` call handles the completion and error paths. Setting `session_manager = None` after closing on pause prevents a redundant close in `finally`.

**Note:** The `session_manager` variable must be accessible from within the pause logic. Since it is declared at the top of `execute_task()` and the pause logic runs inside the same method, this is straightforward. If the pause logic is in a separate method, pass `session_manager` as a parameter or store it on `self` temporarily.

### Step 6: Write schema converter tests

Create `services/worker-service/tests/test_schema_converter.py`:

```python
"""Unit tests for MCP schema conversion to LangChain StructuredTool."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from executor.schema_converter import (
    MAX_TOOLS_PER_AGENT,
    json_schema_to_pydantic,
    mcp_tools_to_structured_tools,
)


class TestJsonSchemaToPydantic:
    def test_empty_schema(self):
        model = json_schema_to_pydantic({})
        assert model is not None
        assert issubclass(model, BaseModel)

    def test_simple_object(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results"},
            },
            "required": ["query"],
        }
        model = json_schema_to_pydantic(schema, "SearchArgs")
        assert model is not None
        # Required field
        fields = model.model_fields
        assert "query" in fields
        assert "limit" in fields

    def test_all_basic_types(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
                "score": {"type": "number"},
                "active": {"type": "boolean"},
            },
        }
        model = json_schema_to_pydantic(schema)
        assert model is not None

    def test_array_type(self):
        schema = {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        }
        model = json_schema_to_pydantic(schema)
        assert model is not None

    def test_nested_object(self):
        schema = {
            "type": "object",
            "properties": {
                "address": {
                    "type": "object",
                    "properties": {
                        "street": {"type": "string"},
                        "city": {"type": "string"},
                    },
                },
            },
        }
        model = json_schema_to_pydantic(schema)
        assert model is not None

    def test_enum_property(self):
        schema = {
            "type": "object",
            "properties": {
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Issue priority",
                },
            },
        }
        model = json_schema_to_pydantic(schema)
        assert model is not None

    def test_default_values(self):
        schema = {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10},
            },
        }
        model = json_schema_to_pydantic(schema)
        instance = model()
        assert instance.limit == 10

    def test_unsupported_ref_returns_none(self):
        schema = {
            "type": "object",
            "$ref": "#/definitions/Foo",
        }
        model = json_schema_to_pydantic(schema)
        assert model is None

    def test_unsupported_allof_returns_none(self):
        schema = {
            "type": "object",
            "properties": {
                "combined": {"allOf": [{"type": "string"}, {"minLength": 1}]},
            },
        }
        model = json_schema_to_pydantic(schema)
        assert model is None

    def test_unsupported_anyof_returns_none(self):
        schema = {
            "type": "object",
            "properties": {
                "value": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            },
        }
        model = json_schema_to_pydantic(schema)
        assert model is None


class TestMcpToolsToStructuredTools:
    def test_empty_schemas(self):
        tools = mcp_tools_to_structured_tools(
            server_name="test-server",
            tool_schemas=[],
            call_fn=AsyncMock(),
        )
        assert tools == []

    def test_single_tool(self):
        schemas = [
            {
                "name": "search",
                "description": "Search for items",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            }
        ]
        tools = mcp_tools_to_structured_tools(
            server_name="test-server",
            tool_schemas=schemas,
            call_fn=AsyncMock(),
        )
        assert len(tools) == 1
        assert tools[0].name == "test-server__search"
        assert tools[0].description == "Search for items"

    def test_namespacing(self):
        schemas = [
            {"name": "tool1", "description": "Tool 1", "inputSchema": {}},
            {"name": "tool2", "description": "Tool 2", "inputSchema": {}},
        ]
        tools = mcp_tools_to_structured_tools(
            server_name="my-server",
            tool_schemas=schemas,
            call_fn=AsyncMock(),
        )
        assert [t.name for t in tools] == ["my-server__tool1", "my-server__tool2"]

    def test_unsupported_schema_skipped(self):
        schemas = [
            {
                "name": "good-tool",
                "description": "Works",
                "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
            },
            {
                "name": "bad-tool",
                "description": "Uses $ref",
                "inputSchema": {"type": "object", "$ref": "#/definitions/Foo"},
            },
        ]
        tools = mcp_tools_to_structured_tools(
            server_name="srv",
            tool_schemas=schemas,
            call_fn=AsyncMock(),
        )
        assert len(tools) == 1
        assert tools[0].name == "srv__good-tool"

    @pytest.mark.asyncio
    async def test_tool_invocation_calls_call_fn(self):
        call_fn = AsyncMock(return_value="result")
        schemas = [
            {
                "name": "do-thing",
                "description": "Does a thing",
                "inputSchema": {
                    "type": "object",
                    "properties": {"input": {"type": "string"}},
                    "required": ["input"],
                },
            }
        ]
        tools = mcp_tools_to_structured_tools(
            server_name="srv",
            tool_schemas=schemas,
            call_fn=call_fn,
        )
        result = await tools[0].ainvoke({"input": "hello"})
        call_fn.assert_called_once_with("srv", "do-thing", {"input": "hello"})
        assert result == "result"

    def test_max_tools_constant(self):
        assert MAX_TOOLS_PER_AGENT == 128
```

### Step 7: Update executor tests

Add tests to `services/worker-service/tests/test_executor.py` for custom tool integration:

- `test_execute_task_no_tool_servers_unchanged` — task without `tool_servers` in agent config behaves identically to current behavior
- `test_execute_task_tool_server_not_found_dead_letters` — referencing a non-existent server → task dead-letters with `tool_server_unavailable`
- `test_execute_task_tool_server_disabled_dead_letters` — referencing a disabled server → task dead-letters
- `test_build_graph_with_custom_tools_merges` — `_build_graph()` with `custom_tools` produces merged tool list
- `test_build_graph_exceeds_tool_limit_raises` — more than 128 total tools raises ValueError

## Acceptance Criteria

- [ ] `schema_converter.py` exists with `json_schema_to_pydantic()` and `mcp_tools_to_structured_tools()`
- [ ] JSON Schema conversion supports: string, integer, number, boolean, array, nested object, enum, default values
- [ ] Unsupported schemas (`$ref`, `allOf`, `anyOf`, `oneOf`, `patternProperties`) cause the tool to be skipped with a warning (not a crash)
- [ ] Custom tools are namespaced as `{server_name}__{tool_name}`
- [ ] `_lookup_tool_server_configs()` queries the `tool_servers` table for server configs
- [ ] Missing or disabled servers cause `McpConnectionError` → task dead-letters via `_handle_dead_letter()` with reason `tool_server_unavailable`
- [ ] Dead-letter call includes `agent_id` for `running_task_count` decrement
- [ ] `McpSessionManager` lifecycle: created before graph, closed in `finally` block
- [ ] MCP sessions are explicitly closed before releasing the lease on task pause (HITL or budget)
- [ ] Custom tool invocations go through `_await_or_cancel()` for cancellation support
- [ ] `_build_graph()` merges built-in and custom tools
- [ ] Total tool count capped at 128; exceeding raises ValueError
- [ ] Tasks without `tool_servers` behave identically to before (no regression)
- [ ] All unit tests pass

## Testing Requirements

- **Unit tests:** Schema conversion for all supported types, unsupported schema detection, tool namespacing, tool invocation wiring, max tool limit. Executor tests for no-tool-server path, server lookup failures, merged graph building.
- **Integration tests:** (Covered by Task 8) Full end-to-end with real MCP server.
- **Regression tests:** Run `make test` — all existing executor tests must still pass.

## Constraints and Guardrails

- Do not modify `_get_tools()` — it continues to handle built-in tools unchanged.
- Do not change the `ToolNode` or `tools_condition` imports — they work with any `StructuredTool` list.
- Do not add new database tables or columns — only read from the existing `tool_servers` table.
- Do not implement MCP server health monitoring or liveness probes.
- Do not add per-tool timeout configuration — use the session manager's default timeout.
- Do not add tool caching — tools are re-discovered on every task start.
- The `_await_or_cancel()` method is already available on `GraphExecutor` — reuse it for custom tool invocations.

## Assumptions

- Task 3 has been completed (`tool_servers` field exists in `agent_config`).
- Task 4 has been completed (`McpSessionManager`, `ToolServerConfig`, `McpConnectionError`, `McpToolCallError` exist in `executor/mcp_session.py`).
- The `tool_servers` table has columns: `name`, `url`, `auth_type`, `auth_token`, `status` (from Task 1).
- `GraphExecutor` has access to `self.pool` (asyncpg connection pool) for database queries.
- The existing `_await_or_cancel()` method signature is: `async def _await_or_cancel(self, coro, cancel_event, *, task_id, operation)`.
- The existing `_handle_dead_letter()` method at line ~998 of `graph.py` handles dead-lettering with lease validation, `running_task_count` decrement, and event insertion. Use it directly — do NOT create a new helper.
- `LangGraph`'s `ToolNode` accepts any list of `StructuredTool` objects and dispatches based on tool name — no special handling needed for custom tools.

<!-- AGENT_TASK_END: task-5-executor-integration.md -->
