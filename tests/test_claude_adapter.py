"""Offline tests for ClaudeCodeAdapter: fixture-driven mapping, flags, env, fake-CLI runs, cancellation."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from orkestr.runtimes.base import RunSpec
from orkestr.runtimes import claude_code as cc
from orkestr.runtimes.claude_code import (
    ClaudeCodeAdapter,
    StreamTranslator,
    build_command,
    build_env,
    guess_context_window,
    iter_events,
)

FIX = Path(__file__).parent / "fixtures" / "claude"


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIX / f"{name}.jsonl").read_text().splitlines() if line.strip()]


def events(name: str):
    return iter_events(load(name))


def of(evs, t):
    return [e for e in evs if e.type == t]


def make_spec(**kw) -> RunSpec:
    base = dict(run_id="r1", agent={"name": "a"}, task="do it", cwd=str(Path.cwd()), model=None,
                system_prompt="", permissions="read", mcp_url=None)
    base.update(kw)
    return RunSpec(**base)


# ----------------------------------------------------------- mapping (fixtures)

def test_plain_answer():
    evs = events("plain")
    assert [e.type for e in evs] == ["session", "text", "usage", "usage", "result"]
    sess = evs[0].data
    assert sess["model"] == "claude-haiku-5-5" and sess["session_id"]
    assert evs[1].data == {"text": "pong", "delta": True}
    res = evs[-1].data
    assert res["text"] == "pong" and res["is_error"] is False
    assert res["cost_usd"] == pytest.approx(0.0007776)
    assert res["num_turns"] == 1


def test_usage_from_message_delta_not_from_assistant_line():
    """With partial messages the assistant line carries the output_tokens of message_start
    (preliminary); the event must use the final message_delta numbers."""
    objs = load("plain_hi")     # real capture: 'Hi there, friend!' -> line says 5, truth is 10
    line_out = next(o for o in objs if o["type"] == "assistant")["message"]["usage"]["output_tokens"]
    final_out = next(o for o in objs if o["type"] == "stream_event" and o["event"]["type"] == "message_delta")["event"]["usage"]["output_tokens"]
    assert final_out > line_out  # the real CLI behaviour that motivated the rule
    usage = of(events("plain_hi"), "usage")[0].data
    assert usage["output_tokens"] == final_out == 10
    u = next(o for o in objs if o["type"] == "assistant")["message"]["usage"]
    assert usage["context_used"] == (u["input_tokens"] + u["cache_read_input_tokens"]
                                     + u["cache_creation_input_tokens"] + final_out)
    # the closing zero-delta event carries the CLI's own context window
    last = of(events("plain_hi"), "usage")[-1].data
    assert (last["input_tokens"], last["output_tokens"]) == (0, 0)
    assert last["context_window"] == 1_000_000 and last["context_used"] == usage["context_used"]


def test_read_file_tool_and_dedupe():
    evs = events("read_file")
    ts, te = of(evs, "tool_start"), of(evs, "tool_end")
    assert len(ts) == 1 and ts[0].data["name"] == "Read" and ts[0].data["mcp"] is None
    assert ts[0].data["input"]["file_path"].endswith("hello.txt")
    assert te[0].data["tool_id"] == ts[0].data["tool_id"] and not te[0].data["is_error"]
    assert "PINEAPPLE" in te[0].data["output"]
    # two API messages -> exactly two token usage events (+ closing window event)
    objs = load("read_file")
    ids = {o["message"]["id"] for o in objs if o["type"] == "assistant"}
    real = [e for e in of(evs, "usage") if e.data["output_tokens"] > 0]
    assert len(ids) == 2 and len(real) == 2


def test_denied_read_outside_cwd_is_tool_error():
    evs = events("read_denied")
    ends = of(evs, "tool_end")
    assert ends[0].data["is_error"] is True and "denied" in ends[0].data["output"].lower()
    assert ends[1].data["is_error"] is False and "PINEAPPLE" in ends[1].data["output"]
    assert of(evs, "result")[0].data["is_error"] is False


def test_edit_attempt_in_read_mode_is_error_the_model_sees():
    evs = events("read_edit_attempt")
    end = of(evs, "tool_end")[0]
    assert end.data["is_error"] and "No such tool" in end.data["output"]


def test_edit_mode_accepts_inside_denies_outside():
    evs = events("edit_accept")
    ends = {e.data["tool_id"]: e.data for e in of(evs, "tool_end")}
    starts = of(evs, "tool_start")
    assert [s.data["name"] for s in starts] == ["Write", "Write"]
    inside, outside = (ends[s.data["tool_id"]] for s in starts)
    assert not inside["is_error"] and "created successfully" in inside["output"]
    assert outside["is_error"] and "denied" in outside["output"].lower()
    # parallel tool_use blocks of one API message share one usage
    objs = load("edit_accept")
    msg_ids = {o["message"]["id"] for o in objs if o["type"] == "assistant"}
    assert len([e for e in of(evs, "usage") if e.data["output_tokens"]]) == len(msg_ids)


def test_mcp_tool_call_maps_server_and_tool():
    evs = events("mcp_call")
    start = of(evs, "tool_start")[0].data
    assert start["name"] == "mcp__orkestr__echo"
    assert start["mcp"] == {"server": "orkestr", "tool": "echo"}
    assert start["input"] == {"text": "abc"}
    end = of(evs, "tool_end")[0].data
    assert "ECHO-OK:abc" in end["output"] and not end["is_error"]


def test_mcp_timeout_is_a_tool_error():
    end = of(events("mcp_timeout"), "tool_end")[0].data
    assert end["is_error"] and "timed out after 2s" in end["output"]


def test_full_mode_bash():
    evs = events("full_bash")
    assert of(evs, "tool_start")[0].data["name"] == "Bash"
    assert of(evs, "tool_end")[0].data["output"].strip() == "FULLMODE-OK"


def test_subagent_attribution_and_no_double_counting():
    objs = load("subagent")
    evs = iter_events(objs)
    starts = of(evs, "tool_start")
    parent = next(s for s in starts if s.data["name"] == "Agent")
    kids = [s for s in starts if s.data.get("parent_tool_use_id")]
    assert kids and all(k.data["parent_tool_use_id"] == parent.data["tool_id"] for k in kids)
    assert "Read" in {k.data["name"] for k in kids}
    assert "parent_tool_use_id" not in parent.data
    # sub-agent tool_end carry the parent id too; Bash is disabled in sub-agents of a read run
    kid_ends = [e for e in of(evs, "tool_end") if e.data.get("parent_tool_use_id")]
    assert len(kid_ends) == len(kids)
    bg = iter_events(load("subagent_background"))
    bash_end = next(e for e in of(bg, "tool_end") if e.data.get("parent_tool_use_id") and e.data["is_error"])
    assert "Bash is disabled" in bash_end.data["output"]
    # one usage per distinct API message id, sub-agent turns without context numbers
    ids = {o["message"]["id"] for o in objs if o["type"] == "assistant"}
    real = [e for e in of(evs, "usage") if e.data["output_tokens"] > 0]
    assert len(real) == len(ids)
    sub_ids = {o["message"]["id"] for o in objs if o["type"] == "assistant" and o.get("parent_tool_use_id")}
    assert sub_ids
    assert sum(1 for e in real if e.data["context_used"] is None) == len(sub_ids)
    # sub-agent prose does not leak into the parent's text
    texts = " ".join(e.data["text"] for e in of(evs, "text"))
    sub_text = [b["text"] for o in objs if o["type"] == "assistant" and o.get("parent_tool_use_id")
                for b in o["message"]["content"] if b["type"] == "text"]
    assert all(t not in texts for t in sub_text)


def test_background_subagent_fixture_has_two_results_and_one_session():
    evs = events("subagent_background")
    assert len(of(evs, "result")) == 2      # raw CLI behaviour; the adapter keeps only the last
    assert len(of(evs, "session")) == 1


def test_streamed_text_is_not_repeated_by_assistant_line():
    evs = events("read_file")
    texts = of(evs, "text")
    assert all(t.data["delta"] for t in texts)
    assert "".join(t.data["text"] for t in texts) == of(evs, "result")[0].data["text"]


def test_non_streamed_text_block_and_thinking():
    tr = StreamTranslator()
    out = tr.feed({"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-4-5", "content": [
        {"type": "thinking", "thinking": "hmm", "signature": "x"}], "usage": {"input_tokens": 1, "output_tokens": 2}}})
    out += tr.feed({"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-4-5", "content": [
        {"type": "text", "text": "hello"}], "usage": {"input_tokens": 1, "output_tokens": 2}}})
    assert [e.type for e in out] == ["thinking", "usage", "text"]
    assert out[0].data == {"text": "hmm", "delta": False}
    assert out[2].data == {"text": "hello", "delta": False}
    assert out[1].data["context_window"] == 200_000  # 4.x model, no CLI data


def test_redacted_thinking_produces_no_event():
    tr = StreamTranslator()
    out = tr.feed({"type": "assistant", "message": {"id": "m", "content": [
        {"type": "thinking", "thinking": "", "signature": "SIG"}], "usage": {}}})
    assert [e.type for e in out] == ["usage"]


def test_tool_result_list_content_and_truncation():
    tr = StreamTranslator()
    tr.feed({"type": "assistant", "message": {"id": "m", "content": [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}})
    big = "x" * 30_000
    out = tr.feed({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": big}, {"type": "image"}],
         "is_error": False}]}})
    assert len(out) == 1 and len(out[0].data["output"]) < 20_100 and out[0].data["output"].endswith("[обрезано]")
    # a repeated line does not repeat the event
    assert tr.feed({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "again"}]}}) == []


def test_error_result_without_text_gets_russian_message():
    tr = StreamTranslator()
    out = tr.feed({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 9})
    res = out[-1]
    assert res.type == "result" and res.data["is_error"] and "лимит" in res.data["text"].lower()


def test_malformed_object_does_not_raise():
    tr = StreamTranslator()
    assert tr.feed({"type": "assistant", "message": {"id": 5, "content": "oops"}}) is not None
    assert tr.feed({"type": "stream_event", "event": None}) == []


def test_context_window_table():
    assert guess_context_window("claude-haiku-5-5") == 1_000_000
    assert guess_context_window("claude-sonnet-4-5") == 200_000
    assert guess_context_window("claude-sonnet-4-5[1m]") == 1_000_000
    assert guess_context_window("claude-opus-4-6") == 200_000
    assert guess_context_window("claude-opus-4-7") == 1_000_000
    assert guess_context_window("opus") == 1_000_000
    assert guess_context_window("some-future-model") == 200_000
    assert guess_context_window(None) == 200_000


def test_window_never_smaller_than_context_used():
    tr = StreamTranslator()
    out = tr.feed({"type": "assistant", "message": {"id": "m", "model": "claude-sonnet-4-5", "content": [],
                                                    "usage": {"input_tokens": 250_000, "output_tokens": 1}}})
    assert out[0].data["context_window"] >= out[0].data["context_used"]


# ------------------------------------------------------------- flags and env

def test_permission_flags_read():
    cmd = build_command("/x/claude", make_spec(permissions="read"), "sid", None)
    assert cmd[:2] == ["/x/claude", "-p"]
    i = cmd.index("--tools")
    tools = set(cmd[i + 1].split(","))
    assert tools == {"Read", "Grep", "Glob", "Agent"}
    assert "--permission-mode" not in cmd and "--dangerously-skip-permissions" not in cmd
    assert "--allowedTools" not in cmd          # would allow Read outside cwd
    assert cmd[cmd.index("--permission-prompts") + 1] == "none"


def test_permission_flags_edit():
    cmd = build_command("c", make_spec(permissions="edit"), "sid", None)
    tools = set(cmd[cmd.index("--tools") + 1].split(","))
    assert {"Edit", "Write", "Read"} <= tools and "Bash" not in tools and "WebFetch" not in tools
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


def test_permission_flags_full():
    cmd = build_command("c", make_spec(permissions="full"), "sid", None)
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--tools" not in cmd


def test_unknown_permission_falls_back_to_read():
    cmd = build_command("c", make_spec(permissions="weird"), "sid", None)  # type: ignore[arg-type]
    assert "Bash" not in cmd[cmd.index("--tools") + 1]


def test_common_flags_model_system_prompt_mcp():
    spec = make_spec(permissions="edit", model="haiku", system_prompt="Be brief.", mcp_url="http://h/mcp/run/tok")
    cmd = build_command("c", spec, "11111111-1111-4111-8111-111111111111", "/tmp/cfg.json")
    for flag in ("--output-format", "stream-json", "--verbose", "--include-partial-messages"):
        assert flag in cmd
    assert cmd[cmd.index("--session-id") + 1] == "11111111-1111-4111-8111-111111111111"
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert cmd[cmd.index("--append-system-prompt") + 1] == "Be brief."
    assert cmd[cmd.index("--mcp-config") + 1] == "/tmp/cfg.json"
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__orkestr"
    assert "--strict-mcp-config" not in cmd      # the user's own MCP servers stay available
    assert "do it" not in cmd                     # the prompt goes through stdin


def test_flags_without_optional_parts():
    cmd = build_command("c", make_spec(), "sid", None, permission_prompts_flag=False)
    assert "--model" not in cmd and "--append-system-prompt" not in cmd
    assert "--mcp-config" not in cmd and "--permission-prompts" not in cmd


def test_custom_mcp_server_name():
    spec = make_spec(mcp_url="http://h", mcp_server_name="team")
    cmd = build_command("c", spec, "s", "/c.json")
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__team"
    assert json.loads(cc.mcp_config_json(spec)) == {"mcpServers": {"team": {"type": "http", "url": "http://h"}}}


def test_build_env_strips_nested_markers_and_keeps_auth():
    base = {
        "PATH": "/bin", "HOME": "/h", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_CODE_SESSION_ID": "x", "CLAUDE_CODE_USE_CCR_V2": "true", "CLAUDE_CODE_REMOTE": "true",
        "SESSION_INGRESS_URL": "u", "CLAUDE_SESSION_INGRESS_TOKEN": "t", "CLAUDE_CODE_SSE_PORT": "1",
        "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "b", "CLAUDE_CODE_OAUTH_TOKEN": "o",
        "CLAUDE_CONFIG_DIR": "/cfg", "CLAUDE_CODE_USE_BEDROCK": "1", "HTTPS_PROXY": "p",
    }
    env = build_env({"X": "1", "MCP_TOOL_TIMEOUT": "5"}, mcp_tool_timeout_s=900, base=base)
    for gone in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_USE_CCR_V2",
                 "CLAUDE_CODE_REMOTE", "SESSION_INGRESS_URL", "CLAUDE_SESSION_INGRESS_TOKEN", "CLAUDE_CODE_SSE_PORT"):
        assert gone not in env
    for kept in ("PATH", "HOME", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
                 "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "HTTPS_PROXY"):
        assert kept in env
    assert env["MCP_TOOL_TIMEOUT"] == "5" and env["X"] == "1"        # spec.env wins
    assert build_env(mcp_tool_timeout_s=900, base=base)["MCP_TOOL_TIMEOUT"] == "900000"
    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert base["CLAUDECODE"] == "1"                                  # input untouched


# --------------------------------------------------------------- fake CLI runs

FAKE_CLI = r'''#!{python}
import json, os, signal, subprocess, sys, time

argv = sys.argv[1:]
dump = os.environ.get("FAKE_DUMP")
mode = os.environ.get("FAKE_MODE", "replay")
if argv[:1] == ["--version"]:
    print(os.environ.get("FAKE_VERSION", "9.9.9 (Claude Code)")); sys.exit(0)
if argv[:1] == ["--help"]:
    print("Options:\n  --permission-prompts <target>  who answers"); sys.exit(0)
if argv[:2] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": os.environ.get("FAKE_LOGGED_IN", "1") == "1"}})); sys.exit(0)
prompt = sys.stdin.read()
if dump:
    cfg = None
    if "--mcp-config" in argv:
        p = argv[argv.index("--mcp-config") + 1]
        cfg = {{"path": p, "content": open(p).read(), "mode": oct(os.stat(p).st_mode & 0o777)}}
    json.dump({{"argv": argv, "prompt": prompt, "cwd": os.getcwd(), "env": dict(os.environ), "cfg": cfg}}, open(dump, "w"))
if mode == "replay":
    for line in open(os.environ["FAKE_FIXTURE"]):
        sys.stdout.write(line); sys.stdout.flush()
elif mode == "fail":
    sys.stderr.write("boom: something broke\nsecond line\n"); sys.exit(3)
elif mode == "hang":
    child = subprocess.Popen(["sleep", "300"])
    open(os.environ["FAKE_PIDS"], "w").write(f"{{os.getpid()}} {{child.pid}}")
    print(json.dumps({{"type": "system", "subtype": "init", "session_id": "s", "model": "claude-haiku-5-5"}}), flush=True)
    if os.environ.get("FAKE_IGNORE_TERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(300)
'''


@pytest.fixture
def fake_cli(tmp_path):
    p = tmp_path / "claude"
    p.write_text(FAKE_CLI.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


async def collect(adapter, spec):
    return [e async for e in adapter.run(spec)]


def replay_env(tmp_path, fixture, **extra):
    d = {"FAKE_FIXTURE": str(FIX / f"{fixture}.jsonl"), "FAKE_DUMP": str(tmp_path / "dump.json")}
    d.update(extra)
    return d


async def test_run_replays_fixture_and_passes_everything_correctly(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    cc.OWN_SESSION_IDS.clear()
    spec = make_spec(cwd=str(tmp_path), task="--weird task\nwith lines", model="haiku", permissions="edit",
                     system_prompt="Extra rules", mcp_url="http://127.0.0.1:1/mcp/run/secret-token",
                     mcp_tool_timeout_s=777, env=replay_env(tmp_path, "mcp_call", EXTRA="yes"))
    evs = await collect(ClaudeCodeAdapter(fake_cli), spec)
    types = [e.type for e in evs]
    assert types[0] == "session" and types[-1] == "result" and types.count("result") == 1
    assert "tool_start" in types and "tool_end" in types
    dump = json.loads((tmp_path / "dump.json").read_text())
    argv = dump["argv"]
    assert dump["prompt"] == "--weird task\nwith lines"            # via stdin, intact
    assert dump["cwd"] == str(tmp_path)
    assert "--weird task" not in " ".join(argv)
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--append-system-prompt") + 1] == "Extra rules"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    sid = argv[argv.index("--session-id") + 1]
    assert sid in cc.OWN_SESSION_IDS
    env = dump["env"]
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["ANTHROPIC_API_KEY"] == "dummy" and env["EXTRA"] == "yes"
    assert env["MCP_TOOL_TIMEOUT"] == "777000"
    cfg = dump["cfg"]
    assert cfg["mode"] == "0o600"
    assert json.loads(cfg["content"]) == {"mcpServers": {"orkestr": {"type": "http", "url": "http://127.0.0.1:1/mcp/run/secret-token"}}}
    assert not os.path.exists(cfg["path"])                          # temp config removed afterwards
    assert not os.path.exists(os.path.dirname(cfg["path"]))
    assert "secret-token" not in json.dumps([e.data for e in evs])  # never leaked into events


async def test_run_without_mcp_has_no_config(fake_cli, tmp_path):
    spec = make_spec(cwd=str(tmp_path), env=replay_env(tmp_path, "plain"))
    await collect(ClaudeCodeAdapter(fake_cli), spec)
    argv = json.loads((tmp_path / "dump.json").read_text())["argv"]
    assert "--mcp-config" not in argv and "--allowedTools" not in argv


async def test_only_last_result_of_background_run_is_yielded(fake_cli, tmp_path):
    spec = make_spec(cwd=str(tmp_path), env=replay_env(tmp_path, "subagent_background"))
    evs = await collect(ClaudeCodeAdapter(fake_cli), spec)
    results = of(evs, "result")
    assert len(results) == 1 and evs[-1] is results[0]
    assert "PINEAPPLE" in results[0].data["text"] and results[0].data["cost_usd"] > 0
    assert len(of(evs, "session")) == 1


async def test_nonzero_exit_without_result_reports_stderr_in_russian(fake_cli, tmp_path):
    spec = make_spec(cwd=str(tmp_path), env={"FAKE_MODE": "fail"})
    evs = await collect(ClaudeCodeAdapter(fake_cli), spec)
    assert [e.type for e in evs] == ["error"]
    d = evs[0].data
    assert d["fatal"] is True and "завершился без результата" in d["message"] and "код 3" in d["message"]
    assert "boom: something broke" in d["message"]


async def test_not_logged_in_result_becomes_fatal_russian_error(fake_cli, tmp_path):
    fx = tmp_path / "auth.jsonl"
    fx.write_text("\n".join(json.dumps(o) for o in [
        {"type": "system", "subtype": "init", "session_id": "s", "model": "m"},
        {"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login",
         "num_turns": 1, "total_cost_usd": 0},
    ]) + "\n")
    spec = make_spec(cwd=str(tmp_path), env={"FAKE_FIXTURE": str(fx)})
    evs = await collect(ClaudeCodeAdapter(fake_cli), spec)
    assert evs[-1].type == "error" and evs[-1].data["fatal"]
    assert "claude auth login" in evs[-1].data["message"] and "не авторизован" in evs[-1].data["message"]
    assert not of(evs, "result")


async def test_regular_error_result_is_a_result_with_is_error(fake_cli, tmp_path):
    fx = tmp_path / "e.jsonl"
    fx.write_text(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                              "num_turns": 2, "total_cost_usd": 0.01}) + "\n")
    evs = await collect(ClaudeCodeAdapter(fake_cli), make_spec(cwd=str(tmp_path), env={"FAKE_FIXTURE": str(fx)}))
    assert evs[-1].type == "result" and evs[-1].data["is_error"] and "ошибкой" in evs[-1].data["text"]


async def test_not_installed_is_fatal_with_install_command(tmp_path):
    ad = ClaudeCodeAdapter(str(tmp_path / "nope"))
    evs = await collect(ad, make_spec(cwd=str(tmp_path)))
    assert len(evs) == 1 and evs[0].type == "error" and evs[0].data["fatal"]
    assert "не найден" in evs[0].data["message"] and "claude auth login" in evs[0].data["message"]
    info = await ad.detect()
    assert info.available is False and info.version is None and "не найден" in info.error


async def test_missing_cwd_is_fatal(fake_cli, tmp_path):
    evs = await collect(ClaudeCodeAdapter(fake_cli), make_spec(cwd=str(tmp_path / "gone")))
    assert evs[0].type == "error" and "Папка проекта" in evs[0].data["message"]


async def test_detect_version_models_and_login(fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_VERSION", "2.1.323 (Claude Code)")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    info = await ClaudeCodeAdapter(fake_cli).detect()
    assert info.available and info.version == "2.1.323" and info.path == fake_cli and info.error is None
    ids = [m.id for m in info.models]
    assert {"opus", "sonnet", "haiku"} <= set(ids) and "opus[1m]" in ids
    assert all(m.provider == "anthropic" and m.location == "cloud" and m.context_window for m in info.models)
    assert info.to_dict()["models"][0]["id"] == ids[0]
    monkeypatch.setenv("FAKE_LOGGED_IN", "0")
    out = await ClaudeCodeAdapter(fake_cli).detect()
    assert out.available is False and "claude auth login" in out.error and out.version == "2.1.323"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # zombie counts as dead
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


async def _wait_dead(pids, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not any(_alive(p) for p in pids):
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.parametrize("ignore_term", [False, True])
async def test_cancellation_kills_the_whole_process_group(fake_cli, tmp_path, ignore_term):
    pidfile = tmp_path / "pids"
    env = {"FAKE_MODE": "hang", "FAKE_PIDS": str(pidfile)}
    if ignore_term:
        env["FAKE_IGNORE_TERM"] = "1"
    spec = make_spec(cwd=str(tmp_path), env=env)
    gen = ClaudeCodeAdapter(fake_cli).run(spec)
    first = await asyncio.wait_for(gen.__anext__(), 10)
    assert first.type == "session"
    leader, child = (int(x) for x in pidfile.read_text().split())
    assert _alive(leader) and _alive(child)
    t0 = time.monotonic()
    await gen.aclose()
    elapsed = time.monotonic() - t0
    assert await _wait_dead([leader, child])
    assert elapsed < (8.0 if ignore_term else 2.0)       # SIGKILL only after the 3 s grace


async def test_task_cancellation_kills_group(fake_cli, tmp_path):
    pidfile = tmp_path / "pids"
    spec = make_spec(cwd=str(tmp_path), env={"FAKE_MODE": "hang", "FAKE_PIDS": str(pidfile)})
    seen: list[str] = []

    async def consume():
        async for ev in ClaudeCodeAdapter(fake_cli).run(spec):
            seen.append(ev.type)

    task = asyncio.create_task(consume())
    for _ in range(100):
        if pidfile.exists() and seen:
            break
        await asyncio.sleep(0.1)
    leader, child = (int(x) for x in pidfile.read_text().split())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_dead([leader, child])


def test_signal_module_used():  # keeps the import honest for linters
    assert signal.SIGTERM
