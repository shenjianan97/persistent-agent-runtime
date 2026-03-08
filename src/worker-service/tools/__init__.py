"""Phase 1 co-located MCP server exports."""

from tools.app import SERVER_NAME, SERVER_INSTRUCTIONS, create_tool_server_app
from tools.definitions import (
    TOOL_NAMES,
    ToolDefinition,
    ToolDependencies,
    create_default_dependencies,
    get_tool_definition,
    get_tool_definitions,
    get_tool_output_schema,
    get_tool_schema,
)


def create_mcp_server(*args, **kwargs):
    """Lazy wrapper to avoid importing `tools.server` during package import."""
    from tools.server import create_mcp_server as _create_mcp_server

    return _create_mcp_server(*args, **kwargs)

__all__ = [
    "SERVER_NAME",
    "SERVER_INSTRUCTIONS",
    "TOOL_NAMES",
    "ToolDefinition",
    "ToolDependencies",
    "create_default_dependencies",
    "create_mcp_server",
    "create_tool_server_app",
    "get_tool_definition",
    "get_tool_definitions",
    "get_tool_output_schema",
    "get_tool_schema",
]
