"""Application assembly for the Phase 1 co-located MCP server."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from tools.definitions import ToolDependencies, create_default_dependencies, register_tools


SERVER_NAME = "persistent-agent-runtime-tools"
SERVER_INSTRUCTIONS = (
    "Phase 1 co-located MCP server exposing read-only idempotent tools: "
    "web_search, read_url, calculator."
)


def create_tool_server_app(
    *,
    dependencies: ToolDependencies | None = None,
    name: str = SERVER_NAME,
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "INFO",
) -> FastMCP:
    """Build the FastMCP application for the Phase 1 tool server.

    This function intentionally contains only application assembly so the package can
    be moved into a future standalone `services/mcp-server/` module with minimal changes.
    """
    server = FastMCP(
        name=name,
        instructions=SERVER_INSTRUCTIONS,
        host=host,
        port=port,
        log_level=log_level,
    )
    register_tools(server, dependencies or create_default_dependencies())
    return server
