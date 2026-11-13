"""Interop with the REAL Claude Code CLI (opt-in: ORKESTR_CLAUDE_CLI=1; makes one tiny haiku call).

The CLI is pointed at `/mcp/agent/{id}` (and, in the second test, at a live run token) with
`--mcp-config <file> --strict-mcp-config`, in an empty temp directory, and asked to call
`list_subordinates` and `delegate`. Verified manually on claude 2.1.x (see the engine report).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

from orkestr.mcp_server import build_mcp_app
from orkestr.runtimes.base import RuntimeEvent
from tests.fakes import make_env, wait_until

pytestmark = [
    pytest.mark.skipif(os.environ.get("ORKESTR_CLAUDE_CLI") != "1" or not shutil.which("claude"),
                       reason="set ORKESTR_CLAUDE_CLI=1 with a logged-in `claude` to run"),
]


async def test_real_claude_cli_delegates_through_agent_endpoint(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    async def script(spec):
        yield RuntimeEvent("result", {"text": f"ПОДЧИНЁННЫЙ-ОТВЕТИЛ:{spec.task}", "is_error": False,
                                      "cost_usd": None, "num_turns": 1})

    env = await make_env("fake", tmp_path, script)
    boss, dev = env.agent("Лид"), env.agent("Дев", role="пишет код")
    env.link(boss, dev)
    app = Starlette(routes=[Mount("/mcp", app=build_mcp_app(env.engine, env.store))])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    srv = asyncio.create_task(server.serve())
    await wait_until(lambda: server.started, 10)
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"orkestr": {"type": "http",
                                                          "url": f"http://127.0.0.1:{port}/mcp/agent/{boss['id']}"}}}))
    env_vars = {k: v for k, v in os.environ.items() if k not in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
    env_vars["NO_PROXY"] = "127.0.0.1,localhost"
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", "--model", "haiku", "--mcp-config", str(cfg), "--strict-mcp-config",
        "--allowedTools", "mcp__orkestr__list_subordinates,mcp__orkestr__delegate",
        "--output-format", "stream-json", "--verbose", "--max-turns", "5",
        "Call the tool list_subordinates, then call the tool delegate with agent 'Дев' and task 'ping'. "
        "Finally reply with exactly the text the delegate tool returned.",
        cwd=str(proj), env=env_vars, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 120)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        server.should_exit = True
        await srv
        await env.engine.stop()
    text = out.decode()
    assert "ПОДЧИНЁННЫЙ-ОТВЕТИЛ:ping" in text, (text[-2000:], err.decode()[-1000:])
    roots = env.store.list_runs(root_only=True)
    assert len(roots) == 1 and roots[0]["origin"] == "delegation" and roots[0]["status"] == "ok"
