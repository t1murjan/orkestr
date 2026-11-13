"""Throwaway MCP server (Streamable HTTP, JSON responses) for OpenCode tests.

Plays the part of the Оркестр delegation endpoint: tools ``list_subordinates``,
``delegate(agent, task)`` and ``check_task``.  ``delegate`` sleeps
``--delay`` seconds before answering so long tool calls can be tested.

Run: ``python tests/tools/opencode_mcp_stub.py --port 8814 [--delay 0]``
URL: ``http://127.0.0.1:8814/mcp``
"""
from __future__ import annotations

import argparse
import asyncio
import json

import uvicorn
from mcp.server.mcpserver import MCPServer

CALLS: list[dict] = []


def build(delay: float = 0.0) -> MCPServer:
    srv = MCPServer("orkestr")

    @srv.tool()
    def list_subordinates() -> str:
        """List the subordinates of the calling agent."""
        CALLS.append({"tool": "list_subordinates"})
        return json.dumps([{"name": "worker", "role": "does subtasks", "runtime": "opencode",
                            "model": None, "status": "idle"}])

    @srv.tool()
    async def delegate(agent: str, task: str) -> str:
        """Delegate a task to a subordinate agent and wait for the result."""
        CALLS.append({"tool": "delegate", "agent": agent, "task": task})
        if delay:
            await asyncio.sleep(delay)
        return f"RESULT-FROM-{agent}: выполнено ({task})"

    @srv.tool()
    def check_task(task_id: str, wait_s: int = 0) -> str:
        """Check the state of a delegated task."""
        CALLS.append({"tool": "check_task", "task_id": task_id})
        return json.dumps({"status": "ok", "task_id": task_id})

    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8814)
    ap.add_argument("--delay", type=float, default=0.0)
    a = ap.parse_args()
    srv = build(a.delay)
    app = srv.streamable_http_app(streamable_http_path="/mcp", json_response=True,
                                  stateless_http=True)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
