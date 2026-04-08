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
