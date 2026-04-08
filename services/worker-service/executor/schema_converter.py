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

    # Check for unsupported keywords at top level
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
