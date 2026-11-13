"""Throwaway MCP server (Streamable HTTP, JSON responses) for manual Claude CLI experiments.

Run: .venv/bin/python tests/tools/throwaway_mcp.py [port]   (default 8803)
Tools: echo(text), slow(seconds).
"""
from __future__ import annotations

import asyncio
import sys

import uvicorn
from mcp.server.mcpserver import MCPServer

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8803
mcp = MCPServer("orkestr")


@mcp.tool()
def echo(text: str) -> str:
    """Return the text with a marker, to prove the tool was called."""
    return f"ECHO-OK:{text}"


@mcp.tool()
async def slow(seconds: int) -> str:
    """Wait the given number of seconds, then return DONE."""
    await asyncio.sleep(seconds)
    return f"SLOW-DONE-{seconds}"


if __name__ == "__main__":
    app = mcp.streamable_http_app(json_response=True, stateless_http=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
