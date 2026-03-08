"""FastMCP server assembly for the co-located Phase 1 tool server."""

from __future__ import annotations

import argparse
import asyncio

from mcp.server.fastmcp import FastMCP

from tools.app import SERVER_NAME, create_tool_server_app
from tools.definitions import ToolDependencies
from tools.runtime_logging import get_tools_logger


LOGGER = get_tools_logger()


def create_mcp_server(
    *,
    dependencies: ToolDependencies | None = None,
    name: str = SERVER_NAME,
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "INFO",
) -> FastMCP:
    """Compatibility wrapper for the worker-owned FastMCP server instance."""
    return create_tool_server_app(
        dependencies=dependencies,
        name=name,
        host=host,
        port=port,
        log_level=log_level,
    )


async def run_stdio_server(
    *,
    dependencies: ToolDependencies | None = None,
    name: str = SERVER_NAME,
    log_level: str = "INFO",
) -> None:
    """Run the Phase 1 MCP server over stdio transport."""
    LOGGER.info("starting MCP server over stdio")
    server = create_mcp_server(
        dependencies=dependencies,
        name=name,
        log_level=log_level,
    )
    await server.run_stdio_async()


async def run_http_server(
    *,
    dependencies: ToolDependencies | None = None,
    name: str = SERVER_NAME,
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "INFO",
) -> None:
    """Run the Phase 1 MCP server over local Streamable HTTP."""
    LOGGER.info("starting MCP server over HTTP at http://%s:%s/mcp", host, port)
    server = create_mcp_server(
        dependencies=dependencies,
        name=name,
        host=host,
        port=port,
        log_level=log_level,
    )
    await server.run_streamable_http_async()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Phase 1 MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="Transport to use for serving MCP.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for HTTP transport.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for HTTP transport.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="FastMCP/Uvicorn log level.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Entry point for running the Phase 1 MCP server as a local process."""
    args = build_arg_parser().parse_args(argv)
    if args.transport == "http":
        asyncio.run(
            run_http_server(
                host=args.host,
                port=args.port,
                log_level=args.log_level,
            )
        )
        return
    asyncio.run(run_stdio_server(log_level=args.log_level))


if __name__ == "__main__":
    main()
