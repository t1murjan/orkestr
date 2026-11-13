"""Capture real Claude Code streams for tests/fixtures/claude/ (costs a few cents, run by hand).

    .venv/bin/python tests/tools/capture_claude_fixtures.py [scenario ...]

Starts the throwaway MCP server on port 8803 itself for the MCP scenarios. Uses the same command line
and environment as ClaudeCodeAdapter. Output is scrubbed (paths, ids, e-mail, signatures, bulky init
fields) so the fixtures are small and contain nothing personal.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orkestr.runtimes.base import RunSpec  # noqa: E402
from orkestr.runtimes.claude_code import build_command, build_env, find_claude, mcp_config_json  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "claude"
PORT = 8803
URL = f"http://127.0.0.1:{PORT}/mcp"
PROJ = "/work/proj"

SCENARIOS = {
    "plain": dict(perm="read", task="Reply with exactly the word: pong"),
    "read_file": dict(perm="read", task="Read hello.txt and tell me the secret word. One short sentence."),
    "read_denied": dict(perm="read", task="Use the Read tool on /etc/hostname, then on hello.txt. Report both in one short sentence."),
    "read_edit_attempt": dict(perm="read", task="Create a file named new.txt containing the word hi. If you cannot, say so in one short sentence."),
    "edit_accept": dict(perm="edit", task="Use the Write tool to create new.txt containing the word hi. Then try to Write /tmp/orkestr_outside.txt with hi too. Report both outcomes in one short sentence."),
    "mcp_call": dict(perm="read", mcp=True, task="Call the echo tool of the orkestr MCP server with text 'abc' and report its output in one short sentence."),
    "mcp_timeout": dict(perm="read", mcp=True, timeout_s=2, task="Call the slow tool of the orkestr MCP server with seconds=5 and report the outcome in one short sentence. Do not retry."),
    "subagent": dict(perm="read", task="Use the Agent tool (subagent_type general-purpose) to launch a sub-agent that reads hello.txt and replies with the secret word, and also tries `ls` with Bash. Report the secret word and whether Bash worked in one short sentence."),
    "full_bash": dict(perm="full", task="Run `echo FULLMODE-OK` with the Bash tool and report the output in one short sentence."),
}


def scrub_value(v, repl):
    if isinstance(v, str):
        for a, b in repl:
            v = v.replace(a, b)
        v = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "user@example.com", v)
        return v
    if isinstance(v, list):
        return [scrub_value(x, repl) for x in v]
    if isinstance(v, dict):
        return {k: scrub_value(x, repl) for k, x in v.items()}
    return v


DROP_INIT = {"slash_commands", "terminal_slash_commands", "skills", "plugins", "capabilities", "agents",
             "startup_timing", "scratchpad_path", "messaging_socket_path", "analytics_disabled",
             "product_feedback_disabled", "fast_mode_state", "fast_mode_disabled_reason", "view_mode"}
DROP_RESULT_PREFIXES = ("process_", "time_", "tool_pool", "main_stream", "first_content", "input_attachments_detail",
                        "tool_schema_build_detail", "message_normalization_detail", "request_build_detail", "ttft",
                        "warm_spare", "flag_fetch", "turn_start", "duration_api")


def scrub_line(obj, repl):
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        obj = {k: v for k, v in obj.items() if k not in DROP_INIT}
    if t == "result":
        obj = {k: v for k, v in obj.items() if not k.startswith(DROP_RESULT_PREFIXES)}
    if t in ("active_goal", "autocompact_state"):
        return None
    for key in ("request_id", "input_transformations", "diagnostics", "wire_tool_inputs", "tool_result_meta"):
        obj.pop(key, None)
    if isinstance(obj.get("message"), dict):
        for b in obj["message"].get("content") or []:
            if isinstance(b, dict) and b.get("type") == "thinking" and b.get("signature"):
                b["signature"] = "SIG"
    ev = obj.get("event")
    if isinstance(ev, dict):
        cb = ev.get("content_block")
        if isinstance(cb, dict) and "signature" in cb:
            cb["signature"] = ""
        d = ev.get("delta")
        if isinstance(d, dict) and d.get("type") == "signature_delta":
            d["signature"] = "SIG"
    return scrub_value(obj, repl)


def run_scenario(name: str, cfg: dict, claude: str) -> None:
    work = tempfile.mkdtemp(prefix="orkcap-")
    Path(work, "hello.txt").write_text("The secret word is PINEAPPLE.\nSecond line.\n")
    spec = RunSpec(run_id="cap", agent={}, task=cfg["task"], cwd=work, model="haiku", system_prompt="",
                   permissions=cfg["perm"], mcp_url=URL if cfg.get("mcp") else None,
                   mcp_tool_timeout_s=cfg.get("timeout_s", 900))
    cfgfile = None
    if spec.mcp_url:
        cfgfile = os.path.join(work + "-cfg.json")
        Path(cfgfile).write_text(mcp_config_json(spec))
    sid = "00000000-0000-4000-8000-000000000001"
    import uuid
    real_sid = str(uuid.uuid4())
    cmd = build_command(claude, spec, real_sid, cfgfile)
    t0 = time.time()
    p = subprocess.run(cmd, input=cfg["task"].encode(), cwd=work, env=build_env(mcp_tool_timeout_s=spec.mcp_tool_timeout_s),
                       capture_output=True, timeout=240)
    print(f"{name}: rc={p.returncode} {time.time() - t0:.1f}s stderr={p.stderr[:200]!r}")
    repl = [(real_sid, sid), (work, PROJ), (os.path.realpath(work), PROJ), (str(Path.home()), "/home/user")]
    lines = []
    for raw in p.stdout.decode("utf-8", "replace").splitlines():
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # per-run session ids of sub-agent files etc. all collapse to the same placeholder
        s = scrub_line(obj, repl)
        if s is not None:
            lines.append(json.dumps(s, ensure_ascii=False))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.jsonl").write_text("\n".join(lines) + "\n")
    shutil.rmtree(work, ignore_errors=True)
    if cfgfile:
        os.unlink(cfgfile)
    for f in ("/tmp/orkestr_outside.txt",):
        if os.path.exists(f):
            os.unlink(f)


def main() -> None:
    claude = find_claude()
    if not claude:
        sys.exit("claude not found")
    names = sys.argv[1:] or list(SCENARIOS)
    srv = None
    if any(SCENARIOS[n].get("mcp") for n in names):
        srv = subprocess.Popen([sys.executable, str(ROOT / "tests/tools/throwaway_mcp.py"), str(PORT)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
    try:
        for n in names:
            run_scenario(n, SCENARIOS[n], claude)
    finally:
        if srv:
            srv.terminate()


if __name__ == "__main__":
    main()
