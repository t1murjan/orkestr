"""End-to-end smoke test: real ``opencode`` binary through OpenCodeAdapter.

Starts the scripted OpenAI-compatible mock (port 8804, plays LM Studio) and,
for the ``delegate`` scenario, the throwaway MCP stub (port 8814); runs one
scenario at one permission level with scratch XDG dirs (the user's real
OpenCode config/data are never touched) and prints every RuntimeEvent.

    .venv/bin/python tests/tools/opencode_smoke.py read_file --perm read
    .venv/bin/python tests/tools/opencode_smoke.py delegate --perm read
    .venv/bin/python tests/tools/opencode_smoke.py edit --perm edit --save fixtures.jsonl

Scenarios: plain read_file edit bash webfetch delegate think multi slow
(see tests/tools/mock_openai_server.py).  ``--target PATH`` sets the file the
``edit`` scenario writes (default: inside the work dir).
``--opencode PATH`` points at a specific binary; otherwise ``$PATH``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orkestr.runtimes.base import RunSpec  # noqa: E402
from orkestr.runtimes.opencode import OpenCodeAdapter, local_provider_id  # noqa: E402
import mock_openai_server  # noqa: E402

MOCK_PORT = 8804
MCP_PORT = 8814


def lmstudio_server(port: int = MOCK_PORT) -> dict:
    return {"id": "srv1", "name": "LM Studio", "kind": "lmstudio",
            "base_url": f"http://127.0.0.1:{port}", "enabled": True, "builtin": True,
            "status": {"state": "up", "models": [
                {"id": "mock-model", "state": "loaded", "context_window": 32768,
                 "max_context_window": 32768}]}}


def scratch_env(root: Path) -> dict[str, str]:
    env = {}
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
        (root / sub).mkdir(parents=True, exist_ok=True)
        env[name] = str(root / sub)
    return env


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", default="plain")
    ap.add_argument("--perm", default="edit", choices=["read", "edit", "full"])
    ap.add_argument("--target", default=None)
    ap.add_argument("--opencode", default=None)
    ap.add_argument("--save", default=None, help="write events as JSON lines")
    ap.add_argument("--delay", type=float, default=0.0, help="MCP delegate delay (s)")
    ap.add_argument("--capture", default=None, help="append raw OpenCode SSE events (JSON lines)")
    ap.add_argument("--user-config", default=None,
                    help="JSON written as the scratch user's global opencode.json")
    ap.add_argument("--mcp-timeout", type=int, default=900)
    ap.add_argument("--git", action="store_true",
                    help="make the scratch dir a git repo with the work dir as a sub-folder")
    ap.add_argument("--no-mcp", action="store_true")
    a = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="orkestr-oc-smoke-"))
    work = tmp / "work"
    work.mkdir()
    if a.git:
        subprocess.run(["git", "init", "-q", str(tmp)], check=True)
    (work / "hello.txt").write_text("hello world file\n")
    env = scratch_env(tmp)
    if a.user_config:
        cfg_dir = Path(env["XDG_CONFIG_HOME"]) / "opencode"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "opencode.json").write_text(a.user_config)

    srv = mock_openai_server.make_server(MOCK_PORT, str(tmp / "mock-requests.jsonl"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    mcp_proc = None
    mcp_url = None
    if not a.no_mcp:
        mcp_proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("opencode_mcp_stub.py")),
             "--port", str(MCP_PORT), "--delay", str(a.delay)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        mcp_url = f"http://127.0.0.1:{MCP_PORT}/mcp"
        time.sleep(1.5)

    task = f"[[scn:{a.scenario}]] [[cwd:{work}]]"
    if a.target:
        task += f" [[target:{a.target}]]"
    spec = RunSpec(
        run_id="smoke", agent={"name": "smoke"}, task=task + " сделай задачу", cwd=str(work),
        model=f"{local_provider_id(lmstudio_server())}/mock-model", system_prompt="Ты тестовый агент Оркестра. Отвечай по-русски.",
        permissions=a.perm, mcp_url=mcp_url, mcp_tool_timeout_s=a.mcp_timeout, env=env,
        local_servers=[lmstudio_server()])
    adapter = OpenCodeAdapter(binary=a.opencode, capture_path=a.capture)
    info = await adapter.detect([lmstudio_server()])
    print("detect:", info.available, info.version, info.path, [m.id for m in info.models][:5])
    out_f = open(a.save, "w") if a.save else None
    t0 = time.time()
    try:
        async for ev in adapter.run(spec):
            line = json.dumps({"type": ev.type, "data": ev.data}, ensure_ascii=False)
            print(f"[{time.time() - t0:5.1f}s] {line[:400]}")
            if out_f:
                out_f.write(line + "\n")
    finally:
        if mcp_proc:
            mcp_proc.terminate()
        srv.shutdown()
        if out_f:
            out_f.close()
    print("work dir:", sorted(p.name for p in work.iterdir()), "| scratch:", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
