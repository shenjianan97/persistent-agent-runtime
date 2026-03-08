"""Tiny manual client for testing the local MCP HTTP endpoint."""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual client for the local MCP HTTP server.")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/mcp",
        help="MCP streamable HTTP endpoint URL.",
    )
    parser.add_argument(
        "--tool",
        default="calculator",
        help="Tool name to call after listing tools.",
    )
    parser.add_argument(
        "--arguments",
        default='{"expression":"2 + 3 * 4"}',
        help="JSON object of tool arguments.",
    )
    parser.add_argument(
        "--skip-call",
        action="store_true",
        help="Only list tools without calling one.",
    )
    return parser


async def main_async(url: str, tool: str, arguments: dict[str, object], skip_call: bool) -> None:
    async with streamable_http_client(url) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()
            print(f"Connected to: {init_result.serverInfo.name}")

            tools_result = await session.list_tools()
            print("Tools:")
            for item in tools_result.tools:
                print(f"- {item.name}")

            if skip_call:
                return

            result = await session.call_tool(tool, arguments)
            print("Result:")
            print(json.dumps(result.structuredContent, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        raise SystemExit("--arguments must decode to a JSON object")
    asyncio.run(main_async(args.url, args.tool, arguments, args.skip_call))


if __name__ == "__main__":
    main()
