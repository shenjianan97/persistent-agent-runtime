"""A simple FastMCP test server exposing custom tools for integration tests."""

import asyncio
from mcp.server.fastmcp import FastMCP


def create_custom_tool_test_server(
    name: str = "test-tools",
    host: str = "127.0.0.1",
    port: int = 9100,
) -> FastMCP:
    """Create a FastMCP server with test tools for integration testing."""
    server = FastMCP(
        name=name,
        host=host,
        port=port,
    )

    @server.tool(
        name="echo",
        description="Echoes the input back. Useful for testing tool invocation.",
    )
    async def echo(message: str) -> str:
        """Echo the message back."""
        return f"Echo: {message}"

    @server.tool(
        name="add_numbers",
        description="Adds two numbers together.",
    )
    async def add_numbers(a: int, b: int) -> str:
        """Add two numbers."""
        return str(a + b)

    @server.tool(
        name="get_info",
        description="Returns structured information about a topic.",
    )
    async def get_info(topic: str, include_details: bool = False) -> str:
        """Get info about a topic."""
        result = f"Info about: {topic}"
        if include_details:
            result += " (with details)"
        return result

    return server


if __name__ == "__main__":
    """Run the test server as a standalone subprocess.

    Usage: python custom_tool_test_server.py --host 127.0.0.1 --port 9100
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()

    server = create_custom_tool_test_server(host=args.host, port=args.port)
    asyncio.run(server.run_streamable_http_async())
