"""Offline tests for the OpenCode adapter (no real opencode needed).

Fixtures in tests/fixtures/opencode/ were captured from the real binary
(1.19.5) through ``tests/tools/opencode_smoke.py --capture``:
``<name>.sse.jsonl`` raw ``/event`` stream (noise events such as
``plugin.added`` dropped), ``<name>.events.jsonl`` what the adapter emitted.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from orkestr.runtimes import opencode as oc
from orkestr.runtimes.base import RunSpec, RuntimeEvent

FIX = Path(__file__).parent / "fixtures" / "opencode"


def _spec(tmp_path: Path, **kw) -> RunSpec:
    base = dict(run_id="r1", agent={"name": "dev"}, task="сделай", cwd=str(tmp_path),
                model=None, system_prompt="", permissions="edit", mcp_url=None)
    base.update(kw)
    return RunSpec(**base)


def _server(kind="lmstudio", url="http://127.0.0.1:1234", models=None, name="LM Studio"):
    return {"id": "s", "name": name, "kind": kind, "base_url": url, "enabled": True,
            "status": {"state": "up", "models": models if models is not None else [
                {"id": "qwen/qwen3-coder-30b", "state": "loaded", "context_window": 65536,
                 "max_context_window": 262144, "type": "llm"},
                {"id": "text-embedding-nomic", "state": "available", "type": "embeddings"},
            ]}}


# -- permissions ---------------------------------------------------------

def _flatten(cfg):
    for k, v in cfg.items():
        if isinstance(v, dict):
            for pat, act in v.items():
                yield k, pat, act
        else:
            yield k, "*", v


@pytest.mark.parametrize("level", ["read", "edit", "full"])
def test_permissions_never_ask(level):
    cfg = oc.permission_config(level)
    assert all(act in ("allow", "deny") for _, _, act in _flatten(cfg))
    # keys the defaults set to "ask" must be pinned explicitly
    assert cfg["doom_loop"] in ("allow", "deny")
    assert cfg["external_directory"] in ("allow", "deny")
    rules = oc.permission_rules(level)
    assert rules and all(r["action"] in ("allow", "deny") for r in rules)


def test_permissions_read():
    cfg = oc.permission_config("read")
    assert cfg["*"] == "deny"
    assert cfg["edit"] == "deny" and cfg["bash"] == "deny" and cfg["webfetch"] == "deny"
    assert cfg["read"]["*"] == "allow" and cfg["read"]["*.env"] == "deny"
    assert cfg["orkestr_*"] == "allow"
    assert cfg["external_directory"] == "deny"


def test_permissions_edit():
    cfg = oc.permission_config("edit")
    assert cfg["edit"] == "allow"
    assert cfg["bash"] == "deny" and cfg["webfetch"] == "deny" and cfg["task"] == "deny"
    assert cfg["external_directory"] == "deny"
    assert cfg["*"] == "deny" and cfg["orkestr_*"] == "allow"


def test_edit_narrowed_to_cwd_inside_git_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "pkg" / "app"
    sub.mkdir(parents=True)
    assert oc.worktree_relative(str(sub)) == "pkg/app"
    cfg = oc.permission_config("edit", cwd=str(sub))
    assert cfg["edit"] == {"*": "deny", "pkg/app/*": "allow"}
    assert {"permission": "edit", "pattern": "pkg/app/*", "action": "allow"} in \
        oc.permission_rules("edit", cwd=str(sub))
    # cwd == repository root: the whole worktree is the project
    assert oc.permission_config("edit", cwd=str(tmp_path))["edit"] == "allow"
    # only the edit level is narrowed
    assert oc.permission_config("read", cwd=str(sub))["edit"] == "deny"
    assert oc.permission_config("full", cwd=str(sub))["*"] == "allow"


def test_edit_narrowed_to_cwd_without_git(tmp_path):
    rel = str(tmp_path.resolve()).lstrip("/")
    assert oc.permission_config("edit", cwd=str(tmp_path))["edit"] == {"*": "deny", f"{rel}/*": "allow"}


def test_permissions_full():
    cfg = oc.permission_config("full")
    assert cfg["*"] == "allow" and cfg["external_directory"] == "allow"
    assert cfg["question"] == "deny"      # nobody can answer questions in a headless run


def test_permission_mcp_glob_follows_server_name():
    assert oc.permission_config("read", "my.server")["my_server_*"] == "allow"


def test_session_rules_match_config_order():
    rules = oc.permission_rules("read")
    assert rules[0] == {"permission": "*", "pattern": "*", "action": "deny"}
    assert {"permission": "read", "pattern": "*.env", "action": "deny"} in rules


# -- config --------------------------------------------------------------

def test_config_mcp_timeout_and_url(tmp_path):
    spec = _spec(tmp_path, mcp_url="http://127.0.0.1:8787/mcp/run/tok", mcp_tool_timeout_s=900)
    cfg = oc.build_config(spec)
    m = cfg["mcp"]["orkestr"]
    assert m == {"type": "remote", "url": "http://127.0.0.1:8787/mcp/run/tok",
                 "enabled": True, "timeout": 900_000}
    assert cfg["experimental"]["mcp_timeout"] >= 900_000
    assert cfg["share"] == "disabled" and cfg["autoupdate"] is False
    assert cfg["permission"] == oc.permission_config("edit", cwd=str(tmp_path))


def test_config_without_mcp(tmp_path):
    cfg = oc.build_config(_spec(tmp_path))
    assert "mcp" not in cfg and "provider" not in cfg


def test_config_custom_server_name_and_user_base(tmp_path):
    spec = _spec(tmp_path, mcp_url="http://x/mcp", mcp_server_name="orkestr-2")
    cfg = oc.build_config(spec, base={"theme": "x", "mcp": {"mine": {"type": "local", "command": ["a"]}}})
    assert set(cfg["mcp"]) == {"mine", "orkestr-2"} and cfg["theme"] == "x"


def test_env_construction(tmp_path):
    spec = _spec(tmp_path, env={"FOO": "bar"})
    cfg = oc.build_config(spec)
    env = oc.build_env(spec, cfg, "pw123", {"PWD": "/stale", "PATH": "/bin"})
    assert env["PWD"] == str(tmp_path)          # OpenCode takes its project dir from $PWD
    assert env["FOO"] == "bar" and env["OPENCODE_SERVER_PASSWORD"] == "pw123"
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == cfg
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert "pw123" not in env["OPENCODE_CONFIG_CONTENT"]


# -- local providers -----------------------------------------------------

def test_provider_ids():
    assert oc.local_provider_id(_server()) == "lmstudio"
    assert oc.local_provider_id(_server(url="http://localhost:1234/")) == "lmstudio"
    assert oc.local_provider_id(_server("llamacpp", "http://127.0.0.1:8080")) == "llamacpp"
    assert oc.local_provider_id(_server(url="http://127.0.0.1:5555")) == "lmstudio-5555"
    assert oc.local_provider_id(_server("openai_compat", "http://127.0.0.1:9000/v1")) == "openai-compat-9000"
    assert oc.local_provider_id(_server(url="http://192.168.1.7:1234")) == "lmstudio-192-168-1-7-1234"


def test_provider_injection_from_local_servers(tmp_path):
    llama = _server("llamacpp", "http://127.0.0.1:8080/", models=[
        {"id": "qwen.gguf", "context_window": None, "max_context_window": 8192}], name="llama.cpp")
    compat = _server("openai_compat", "http://127.0.0.1:9000/v1", models=[{"id": "m1"}], name="Mine")
    spec = _spec(tmp_path, local_servers=[_server(), llama, compat])
    prov = oc.build_config(spec)["provider"]
    assert set(prov) == {"lmstudio", "llamacpp", "openai-compat-9000"}
    lm = prov["lmstudio"]
    assert lm["npm"] == "@ai-sdk/openai-compatible"
    assert lm["options"]["baseURL"] == "http://127.0.0.1:1234/v1"
    assert list(lm["models"]) == ["qwen/qwen3-coder-30b"]               # embeddings skipped
    assert lm["models"]["qwen/qwen3-coder-30b"]["limit"] == {"context": 65536, "output": 16384}
    assert lm["models"]["qwen/qwen3-coder-30b"]["tool_call"] is True
    assert prov["llamacpp"]["options"]["baseURL"] == "http://127.0.0.1:8080/v1"
    assert prov["llamacpp"]["models"]["qwen.gguf"]["limit"]["context"] == 8192
    assert prov["openai-compat-9000"]["options"]["baseURL"] == "http://127.0.0.1:9000/v1"
    assert "apiKey" in lm["options"] and lm["options"]["apiKey"] == "local"   # dummy, not a secret


def test_provider_not_injected_when_user_defines_it(tmp_path):
    spec = _spec(tmp_path, local_servers=[_server()])
    assert "provider" not in oc.build_config(spec, user_providers={"lmstudio"})


def test_wanted_model_missing_from_server_list_is_added(tmp_path):
    spec = _spec(tmp_path, local_servers=[_server()], model="lmstudio/other/model")
    assert "other/model" in oc.build_config(spec)["provider"]["lmstudio"]["models"]


def test_other_server_kinds_ignored(tmp_path):
    srv = {"id": "a", "kind": "anthropic", "base_url": "https://api.anthropic.com", "status": None}
    assert "provider" not in oc.build_config(_spec(tmp_path, local_servers=[srv]))


def test_user_defined_providers(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    (xdg / "opencode").mkdir(parents=True)
    (xdg / "opencode" / "opencode.json").write_text('{"provider": {"ollama": {}}}')
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / "opencode.jsonc").write_text(
        '{\n // comment with "quotes"\n "provider": {"lmstudio": {"name": "x // y"},}, /* c */\n}')
    env = {"XDG_CONFIG_HOME": str(xdg), "HOME": str(tmp_path),
           "OPENCODE_CONFIG_CONTENT": '{"provider": {"inline": {}}}'}
    got = oc.user_defined_providers(str(proj / "sub"), env)
    assert got == {"ollama", "lmstudio", "inline"}


def test_strip_jsonc_keeps_strings():
    assert json.loads(oc.strip_jsonc('{"a": "http://x//y", /* z */ "b": [1,2,],}')) == {
        "a": "http://x//y", "b": [1, 2]}


# -- event mapping from real captures ------------------------------------

def _load(name):
    return [json.loads(line) for line in (FIX / name).read_text().splitlines() if line.strip()]


def _session_of(rows):
    for r in rows:
        p = r["properties"]
        sid = p.get("sessionID") or (p.get("info") or {}).get("sessionID") \
            or (p.get("part") or {}).get("sessionID")
        if sid:
            return sid
    raise AssertionError("no session id in fixture")


def _replay(name, ctx=32768, model="lmstudio-8804/mock-model"):
    rows = _load(name + ".sse.jsonl")
    m = oc.EventMapper(_session_of(rows), ["orkestr"], ctx, model)
    out = m._session_event()
    for r in rows:
        out.extend(m.feed(r))
    return m, [{"type": e.type, "data": e.data} for e in out]


FIXTURES = ["plain", "think", "read_file", "edit_ok", "edit_denied_outside",
            "edit_denied_read", "bash_full", "delegate"]


@pytest.mark.parametrize("name", FIXTURES)
def test_replay_matches_what_the_real_run_emitted(name):
    _, got = _replay(name)
    expected = [e for e in _load(name + ".events.jsonl") if e["type"] != "result"]
    assert got == expected


def test_plain_text_usage_and_streaming():
    m, ev = _replay("plain")
    types = [e["type"] for e in ev]
    assert types[0] == "session" and ev[0]["data"]["context_window"] == 32768
    texts = [e for e in ev if e["type"] == "text"]
    assert texts and all(t["data"]["delta"] for t in texts)
    assert "".join(t["data"]["text"] for t in texts) == "Привет! Это ответ мок-модели."
    (usage,) = [e["data"] for e in ev if e["type"] == "usage"]
    assert usage["input_tokens"] == 200 and usage["output_tokens"] == 9
    assert usage["context_used"] == 209 and usage["context_window"] == 32768
    assert usage["cost_usd"] == 0.0
    assert m.steps == 1


def test_thinking_events():
    _, ev = _replay("think")
    th = "".join(e["data"]["text"] for e in ev if e["type"] == "thinking")
    assert th.strip() == "Сначала подумаю. Потом отвечу."


def test_mcp_tool_call_is_tagged():
    _, ev = _replay("delegate")
    start = next(e["data"] for e in ev if e["type"] == "tool_start")
    assert start["name"] == "orkestr_delegate"
    assert start["mcp"] == {"server": "orkestr", "tool": "delegate"}
    assert start["input"]["agent"] == "worker"
    end = next(e["data"] for e in ev if e["type"] == "tool_end")
    assert end["tool_id"] == start["tool_id"] and not end["is_error"]
    assert end["output"].startswith("RESULT-FROM-worker")


def test_builtin_tool_has_no_mcp_and_file_path_alias():
    _, ev = _replay("read_file")
    start = next(e["data"] for e in ev if e["type"] == "tool_start")
    assert start["name"] == "read" and start["mcp"] is None
    assert start["input"]["file_path"] == start["input"]["filePath"]


def test_denied_tool_results_are_errors_the_model_sees():
    _, ev = _replay("edit_denied_outside")
    end = next(e["data"] for e in ev if e["type"] == "tool_end")
    assert end["is_error"] and "prevents you from using this specific tool call" in end["output"]
    _, ev = _replay("edit_denied_read")
    end = next(e["data"] for e in ev if e["type"] == "tool_end")
    assert end["is_error"] and "unavailable tool" in end["output"]


def test_tool_start_before_end_and_once():
    for name in ("read_file", "edit_ok", "bash_full", "delegate"):
        _, ev = _replay(name)
        starts = [e["data"]["tool_id"] for e in ev if e["type"] == "tool_start"]
        ends = [e["data"]["tool_id"] for e in ev if e["type"] == "tool_end"]
        assert starts == ends and len(set(starts)) == len(starts)
        assert [e["type"] for e in ev].index("tool_start") < [e["type"] for e in ev].index("tool_end")


def test_model_and_window_learned_from_first_assistant_message():
    rows = _load("plain.sse.jsonl")
    m = oc.EventMapper(_session_of(rows), [], None, None, model_resolver=lambda mid: 4096)
    out = []
    for r in rows:
        out.extend(m.feed(r))
    sess = [e for e in out if e.type == "session"]
    assert len(sess) == 1 and sess[0].data["model"] == "lmstudio-8804/mock-model"
    assert sess[0].data["context_window"] == 4096
    usage = [e for e in out if e.type == "usage"][0]
    assert usage.data["context_window"] == 4096


def test_events_of_other_sessions_are_ignored():
    rows = _load("plain.sse.jsonl")
    m = oc.EventMapper("ses_other", [], 100, "x/y")
    assert [e for r in rows for e in m.feed(r)] == []


def test_retry_status_is_tracked():
    rows = _load("server_down_retry.sse.jsonl")
    m = oc.EventMapper(_session_of(rows), [], None, None)
    logs = [e for r in rows for e in m.feed(r) if e.type == "log"]
    assert logs and m.retry_attempt >= 3 and "ECONNREFUSED" in m.retry_message


def test_permission_asked_is_logged():
    rows = _load("permission_asked.sse.jsonl")
    m = oc.EventMapper(_session_of(rows), [], None, None)
    evs = [e for r in rows for e in m.feed(r)]
    assert m.permission_requests and any(e.type == "log" and "отклонено" in e.data["message"] for e in evs)


def test_text_part_without_deltas_is_emitted_whole():
    m = oc.EventMapper("ses_1", [], None, "p/m")
    m.feed({"type": "message.updated", "properties": {"sessionID": "ses_1", "info": {
        "id": "msg_a", "sessionID": "ses_1", "role": "assistant", "providerID": "p", "modelID": "m"}}})
    out = m.feed({"type": "message.part.updated", "properties": {"sessionID": "ses_1", "part": {
        "id": "prt_1", "sessionID": "ses_1", "messageID": "msg_a", "type": "text", "text": "целиком",
        "time": {"start": 1, "end": 2}}}})
    assert [(e.type, e.data) for e in out if e.type == "text"] == [
        ("text", {"text": "целиком", "delta": False})]
    # a repeated final update must not duplicate it
    again = m.feed({"type": "message.part.updated", "properties": {"sessionID": "ses_1", "part": {
        "id": "prt_1", "sessionID": "ses_1", "messageID": "msg_a", "type": "text", "text": "целиком",
        "time": {"start": 1, "end": 2}}}})
    assert [e for e in again if e.type == "text"] == []


def test_user_message_text_is_not_an_assistant_text():
    m = oc.EventMapper("ses_1", [], None, "p/m")
    m.feed({"type": "message.updated", "properties": {"sessionID": "ses_1", "info": {
        "id": "msg_u", "sessionID": "ses_1", "role": "user"}}})
    out = m.feed({"type": "message.part.updated", "properties": {"sessionID": "ses_1", "part": {
        "id": "prt_u", "sessionID": "ses_1", "messageID": "msg_u", "type": "text", "text": "задача",
        "time": {"start": 1, "end": 2}}}})
    assert [e for e in out if e.type == "text"] == []


def test_parts_wait_for_message_role():
    m = oc.EventMapper("ses_1", [], None, "p/m")
    part = {"id": "prt_1", "sessionID": "ses_1", "messageID": "msg_a", "type": "tool", "callID": "c1",
            "tool": "read", "state": {"status": "running", "input": {"filePath": "/x"}, "time": {"start": 1}}}
    assert m.feed({"type": "message.part.updated", "properties": {"sessionID": "ses_1", "part": part}}) == []
    out = m.feed({"type": "message.updated", "properties": {"sessionID": "ses_1", "info": {
        "id": "msg_a", "sessionID": "ses_1", "role": "assistant", "providerID": "p", "modelID": "m"}}})
    assert [e.type for e in out] == ["session", "tool_start"]


def test_tool_output_truncated():
    m = oc.EventMapper("ses_1", [], None, "p/m")
    m._roles["msg_a"] = "assistant"
    part = {"id": "prt_1", "sessionID": "ses_1", "messageID": "msg_a", "type": "tool", "callID": "c1",
            "tool": "bash", "state": {"status": "completed", "input": {}, "output": "x" * 50_000,
                                      "title": "t", "metadata": {}, "time": {"start": 1, "end": 2}}}
    out = m.feed({"type": "message.part.updated", "properties": {"sessionID": "ses_1", "part": part}})
    end = next(e for e in out if e.type == "tool_end")
    assert len(end.data["output"]) < 20_100


def test_usage_reasoning_and_cache_are_folded():
    m = oc.EventMapper("ses_1", [], 1000, "p/m")
    m._roles["msg_a"] = "assistant"
    part = {"id": "prt_f", "sessionID": "ses_1", "messageID": "msg_a", "type": "step-finish",
            "reason": "stop", "cost": 0.0123,
            "tokens": {"input": 100, "output": 20, "reasoning": 5, "cache": {"read": 50, "write": 7}}}
    out = m.feed({"type": "message.part.updated", "properties": {"sessionID": "ses_1", "part": part}})
    u = next(e.data for e in out if e.type == "usage")
    assert u == {"input_tokens": 100, "output_tokens": 25, "cache_read_tokens": 50,
                 "cache_write_tokens": 7, "context_used": 182, "context_window": 1000,
                 "cost_usd": 0.0123}
    assert m.cost == pytest.approx(0.0123)


# -- error texts ---------------------------------------------------------

def test_describe_error_variants():
    assert "не удаётся подключиться" in oc.describe_error(
        {"name": "APIError", "data": {"message": "Cannot connect to API: ECONNREFUSED"}})
    assert "HTTP 429" in oc.describe_error(
        {"name": "APIError", "data": {"message": "slow down", "statusCode": 429}})
    assert "opencode auth login" in oc.describe_error(
        {"name": "ProviderAuthError", "data": {"providerID": "anthropic", "message": "нет ключа"}})
    assert "переполнен" in oc.describe_error({"name": "ContextOverflowError", "data": {"message": "x"}})
    assert oc.describe_error({"name": "UnknownError", "data": {"message": "boom"}}) == "boom"


# -- model list parsing --------------------------------------------------

def test_parse_models_verbose_fixture():
    items = oc.parse_models_verbose((FIX / "models_verbose.txt").read_text())
    refs = [i["_ref"] for i in items]
    assert "opencode/big-pickle" in refs and "lmstudio/qwen/qwen3-coder-30b" in refs
    big = next(i for i in items if i["_ref"] == "opencode/big-pickle")
    assert big["limit"]["context"] > 0 and big["name"]


# -- fake CLI: detect, failures, cancellation -----------------------------

FAKE_CLI = r'''#!__PY__
import json, os, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "hang")
if args[:1] == ["--version"]:
    print("9.9.9"); sys.exit(0)
if args[:1] == ["models"]:
    sys.stdout.write(open(os.environ["FAKE_MODELS"]).read()); sys.exit(0)
if args[:1] != ["serve"]:
    sys.exit(2)
if mode == "die":
    sys.stderr.write("boom: cannot bind\n"); sys.exit(3)
port = int(args[args.index("--port") + 1])
child = subprocess.Popen(["sleep", "300"])
open(os.environ["FAKE_PIDFILE"], "w").write(f"{os.getpid()} {child.pid}")
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj, code=200):
        d = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
    def do_GET(self):
        if self.path.startswith("/global/health"): return self._send({"healthy": True})
        if self.path.startswith("/config/providers"): return self._send({"providers": [{"id": "fake", "models": {"m": {"limit": {"context": 1234}}}}], "default": {}})
        if self.path.startswith("/event"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write(b'data: {"type":"server.connected","properties":{}}\n\n'); self.wfile.flush()
            if mode == "retry":
                for i in (1, 2, 3):
                    ev = {"type": "session.status", "properties": {"sessionID": "ses_fake", "status": {
                        "type": "retry", "attempt": i, "message": "Cannot connect to API: ECONNREFUSED"}}}
                    self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode()); self.wfile.flush()
            time.sleep(300); return
        self._send({}, 404)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        if self.path.startswith("/session/ses_fake/message"): time.sleep(300); return
        if self.path.startswith("/session/ses_fake/abort"): return self._send(True)
        if self.path.startswith("/session"): return self._send({"id": "ses_fake"})
        self._send({}, 404)
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


@pytest.fixture
def fake_cli(tmp_path):
    p = tmp_path / "bin" / "opencode"
    p.parent.mkdir()
    p.write_text(FAKE_CLI.replace("__PY__", sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # zombies count as dead
        return Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[0] != "Z"
    except OSError:
        return False


async def _collect(agen, limit=50):
    out = []
    async for ev in agen:
        out.append(ev)
        if len(out) >= limit:
            break
    return out


async def test_detect_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ORKESTR_OPENCODE_BIN", raising=False)
    info = await oc.OpenCodeAdapter().detect([])
    assert not info.available and "npm install -g opencode-ai" in info.error and "не установлен" in info.error


async def test_run_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ORKESTR_OPENCODE_BIN", raising=False)
    evs = await _collect(oc.OpenCodeAdapter().run(_spec(tmp_path)))
    assert len(evs) == 1 and evs[0].type == "error" and evs[0].data["fatal"]
    assert "npm install -g opencode-ai" in evs[0].data["message"]


async def test_run_bad_cwd(tmp_path, fake_cli):
    evs = await _collect(oc.OpenCodeAdapter(binary=str(fake_cli)).run(
        _spec(tmp_path, cwd=str(tmp_path / "nope"))))
    assert evs[0].type == "error" and "не найдена" in evs[0].data["message"]


async def test_detect_with_fake_cli(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_MODELS", str(FIX / "models_verbose.txt"))
    servers = [_server(), _server("llamacpp", "http://127.0.0.1:8080", name="llama.cpp",
                                  models=[{"id": "gguf-a", "max_context_window": 4096}])]
    t0 = time.monotonic()
    info = await oc.OpenCodeAdapter(binary=str(fake_cli)).detect(servers)
    assert time.monotonic() - t0 < 10
    assert info.available and info.version == "9.9.9" and info.path == str(fake_cli)
    by_id = {m.id: m for m in info.models}
    big = by_id["opencode/big-pickle"]
    assert big.location == "cloud" and big.provider == "opencode" and big.context_window
    # live data from the passed servers replaces OpenCode's own lmstudio catalog entries
    assert "lmstudio/openai/gpt-oss-20b" not in by_id and "lmstudio/mock-model" not in by_id
    lm = by_id["lmstudio/qwen/qwen3-coder-30b"]
    assert lm.location == "local" and lm.context_window == 65536 and lm.provider == "lmstudio"
    assert "lmstudio/text-embedding-nomic" not in by_id
    assert by_id["llamacpp/gguf-a"].context_window == 4096
    assert info.to_dict()["models"][0]["id"]


async def test_startup_failure_surfaces_stderr(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "die")
    monkeypatch.setenv("FAKE_PIDFILE", str(tmp_path / "pids"))
    evs = await _collect(oc.OpenCodeAdapter(binary=str(fake_cli)).run(_spec(tmp_path)))
    assert len(evs) == 1 and evs[0].type == "error" and evs[0].data["fatal"]
    assert "завершился при запуске" in evs[0].data["message"] and "boom: cannot bind" in evs[0].data["message"]


async def test_cancel_kills_process_group(tmp_path, fake_cli, monkeypatch):
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    adapter = oc.OpenCodeAdapter(binary=str(fake_cli))
    seen: list[RuntimeEvent] = []

    async def consume():
        async for ev in adapter.run(_spec(tmp_path, model="fake/m")):
            seen.append(ev)

    task = asyncio.create_task(consume())
    for _ in range(100):
        await asyncio.sleep(0.1)
        if pidfile.exists() and pidfile.read_text().count(" ") and any(e.type == "session" for e in seen):
            break
    srv_pid, child_pid = map(int, pidfile.read_text().split())
    assert _alive(srv_pid) and _alive(child_pid)
    t0 = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - t0 < 6
    await asyncio.sleep(0.2)
    assert not _alive(srv_pid) and not _alive(child_pid)


async def test_aclose_kills_process_group(tmp_path, fake_cli, monkeypatch):
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    gen = oc.OpenCodeAdapter(binary=str(fake_cli)).run(_spec(tmp_path, model="fake/m"))
    first = await gen.__anext__()        # session event
    assert first.type == "session" and first.data["context_window"] == 1234
    srv_pid, child_pid = map(int, pidfile.read_text().split())
    await gen.aclose()
    await asyncio.sleep(0.2)
    assert not _alive(srv_pid) and not _alive(child_pid)


async def test_api_retries_become_fatal_error(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "retry")
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    evs = await _collect(oc.OpenCodeAdapter(binary=str(fake_cli)).run(_spec(tmp_path, model="fake/m")))
    last = evs[-1]
    assert last.type == "error" and last.data["fatal"]
    assert "Модель не отвечает" in last.data["message"] and "LM Studio" in last.data["message"]
    assert not any(e.type == "result" for e in evs)
    await asyncio.sleep(0.2)
    srv_pid, child_pid = map(int, pidfile.read_text().split())
    assert not _alive(srv_pid) and not _alive(child_pid)


async def test_unknown_model_is_reported_in_russian(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_PIDFILE", str(tmp_path / "pids"))
    evs = await _collect(oc.OpenCodeAdapter(binary=str(fake_cli)).run(
        _spec(tmp_path, model="nope/zzz")))
    assert evs[-1].type == "error" and "не найдена в OpenCode" in evs[-1].data["message"]
