"""Tests that drive the real ``opencode`` binary through the adapter.

Skipped automatically when ``opencode`` is not installed.  They use scratch
XDG dirs (the user's OpenCode config/data are never touched), the scripted
OpenAI-compatible mock on port 8804 and the throwaway MCP stub on port 8814.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from orkestr.runtimes import opencode as oc
from orkestr.runtimes.base import RunSpec

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import mock_openai_server  # noqa: E402

BIN = oc.find_binary()
pytestmark = pytest.mark.skipif(BIN is None, reason="opencode is not installed")

MOCK_PORT = 8804
MCP_PORT = 8814
SERVER = {"id": "s1", "name": "LM Studio", "kind": "lmstudio",
          "base_url": f"http://127.0.0.1:{MOCK_PORT}", "enabled": True,
          "status": {"state": "up", "models": [
              {"id": "mock-model", "state": "loaded", "context_window": 32768}]}}
MODEL = f"{oc.local_provider_id(SERVER)}/mock-model"


@pytest.fixture(scope="module")
def mock_server():
    srv = mock_openai_server.make_server(MOCK_PORT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def mcp_stub():
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "tools" / "opencode_mcp_stub.py"),
         "--port", str(MCP_PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    yield f"http://127.0.0.1:{MCP_PORT}/mcp"
    proc.terminate()
    proc.wait(5)


def _env(tmp: Path) -> dict[str, str]:
    env = {}
    for name, sub in (("XDG_CONFIG_HOME", "c"), ("XDG_DATA_HOME", "d"),
                      ("XDG_STATE_HOME", "s"), ("XDG_CACHE_HOME", "k")):
        (tmp / sub).mkdir(exist_ok=True)
        env[name] = str(tmp / sub)
    return env


async def _run(tmp: Path, scenario: str, perm: str, mcp_url=None, extra="") -> list:
    work = tmp / "work"
    work.mkdir(exist_ok=True)
    (work / "hello.txt").write_text("hello world file\n")
    spec = RunSpec(run_id="t", agent={"name": "t"}, cwd=str(work), model=MODEL, permissions=perm,
                   task=f"[[scn:{scenario}]] [[cwd:{work}]] {extra} go", mcp_url=mcp_url,
                   system_prompt="Ты тестовый агент.", env=_env(tmp), local_servers=[SERVER])
    return [ev async for ev in oc.OpenCodeAdapter(binary=BIN, startup_timeout_s=90).run(spec)]


def _types(evs):
    return [e.type for e in evs]


async def test_detect_real(tmp_path, monkeypatch):
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)
    info = await oc.OpenCodeAdapter(binary=BIN).detect([SERVER])
    assert info.available and info.version and info.path == BIN
    assert any(m.id == MODEL and m.location == "local" and m.context_window == 32768
               for m in info.models)


async def test_plain_answer(tmp_path, mock_server):
    evs = await _run(tmp_path, "plain", "read")
    assert _types(evs)[0] == "session" and _types(evs)[-1] == "result"
    assert evs[0].data["model"] == MODEL and evs[0].data["context_window"] == 32768
    assert evs[-1].data["text"] == "Привет! Это ответ мок-модели."
    usage = [e for e in evs if e.type == "usage"]
    assert usage and usage[0].data["input_tokens"] > 0 and usage[0].data["context_used"]


async def test_edit_level_writes_inside_denies_outside(tmp_path, mock_server):
    evs = await _run(tmp_path, "edit", "edit")
    assert (tmp_path / "work" / "created_by_agent.txt").exists()
    assert not next(e for e in evs if e.type == "tool_end").data["is_error"]
    outside = tmp_path / "outside.txt"
    evs = await _run(tmp_path, "edit", "edit", extra=f"[[target:{outside}]]")
    end = next(e for e in evs if e.type == "tool_end")
    assert end.data["is_error"] and not outside.exists()
    assert evs[-1].type == "result"          # the model saw the error and finished; no hang


async def test_edit_level_is_limited_to_cwd_inside_a_git_repo(tmp_path, mock_server):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    sibling = tmp_path / "sibling.txt"
    evs = await _run(tmp_path, "edit", "edit", extra=f"[[target:{sibling}]]")
    assert next(e for e in evs if e.type == "tool_end").data["is_error"] and not sibling.exists()
    (work / "sub").mkdir()
    evs = await _run(tmp_path, "edit", "edit", extra=f"[[target:{work}/sub/new.txt]]")
    assert not next(e for e in evs if e.type == "tool_end").data["is_error"]
    assert (work / "sub" / "new.txt").exists()


@pytest.mark.parametrize("scenario", ["edit", "bash", "webfetch"])
async def test_read_level_denies_everything_risky(tmp_path, mock_server, scenario):
    evs = await _run(tmp_path, scenario, "read")
    end = next(e for e in evs if e.type == "tool_end")
    assert end.data["is_error"]
    assert not (tmp_path / "work" / "created_by_agent.txt").exists()
    assert evs[-1].type == "result"


async def test_edit_level_has_no_shell(tmp_path, mock_server):
    evs = await _run(tmp_path, "bash", "edit")
    assert next(e for e in evs if e.type == "tool_end").data["is_error"]


async def test_full_level_allows_shell(tmp_path, mock_server):
    evs = await _run(tmp_path, "bash", "full")
    end = next(e for e in evs if e.type == "tool_end")
    assert not end.data["is_error"] and "shell-ran" in end.data["output"]


async def test_mcp_round_trip(tmp_path, mock_server, mcp_stub):
    for perm in ("read", "edit", "full"):
        evs = await _run(tmp_path, "delegate", perm, mcp_url=mcp_stub)
        start = next(e for e in evs if e.type == "tool_start")
        assert start.data["mcp"] == {"server": "orkestr", "tool": "delegate"}
        end = next(e for e in evs if e.type == "tool_end")
        assert not end.data["is_error"] and "RESULT-FROM-worker" in end.data["output"]
        assert "RESULT-FROM-worker" in evs[-1].data["text"]


async def test_dead_mcp_url_is_a_clear_error(tmp_path, mock_server):
    evs = await _run(tmp_path, "plain", "read", mcp_url="http://127.0.0.1:8899/mcp")
    assert evs[-1].type == "error" and evs[-1].data["fatal"]
    assert "делегирования" in evs[-1].data["message"]
