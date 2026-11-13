"""HTTP API: every endpoint of CONTRACT §4, error shapes, SSE (§5). Uses fakes for engine/probes."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import types

import httpx
import pytest
from starlette.testclient import TestClient

from orkestr.app import create_app
from orkestr.config import Config
from orkestr.interfaces import EngineError


class FakeEngine:
    def __init__(self, store) -> None:
        self.store = store
        self.started = self.stopped = False
        self.cancelled: list[str] = []
        self.refreshed = 0

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def start_run(self, agent_id, task, *, cwd=None, origin="user", parent_run_id=None, parent_step_id=None):
        agent = self.store.get_agent(agent_id)
        if agent["name"] == "broken":
            raise EngineError("runtime_unavailable", "Среда не найдена")
        return self.store.create_run({"agent_id": agent_id, "task": task, "status": "running",
                                      "cwd": cwd, "origin": origin})

    async def cancel_run(self, run_id):
        self.cancelled.append(run_id)
        self.store.update_run(run_id, {"status": "cancelled", "ended_at": int(time.time() * 1000)})
        return True

    def runtimes_info(self):
        return [{"id": "demo", "label": "Демо", "available": True, "version": None, "path": None,
                 "error": None, "models": []}]

    async def refresh_runtimes(self):
        self.refreshed += 1
        return self.runtimes_info()


class FakeProbes:
    def __init__(self, store) -> None:
        self.store = store
        self.started = self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def check_server(self, server_id):
        return self.store.save_server_status(server_id, {"state": "up", "checked_at": int(time.time() * 1000),
                                                          "latency_ms": 3, "error": None, "version": None,
                                                          "models": [], "slots": [], "metrics": None})

    def latest_hardware(self):
        return None

    def local_servers_up(self):
        return []


def make_app(tmp_path, **kw):
    web = tmp_path / "web"
    web.mkdir(exist_ok=True)
    (web / "index.html").write_text("<html>index</html>")
    (web / "app.js").write_text("console.log(1)")
    (web / "logo.png").write_bytes(b"\x89PNG")
    cfg = Config(db_path=str(tmp_path / "api.db"), web_dir=web)
    app = create_app(cfg, engine=False, probes=False, watcher=False, driver=False, mcp=False, **kw)
    return app


@pytest.fixture
def app(tmp_path):
    from orkestr.bus import EventBus
    from orkestr.store import SqliteStore
    bus = EventBus()
    store = SqliteStore(str(tmp_path / "api.db"), bus)
    eng, pr = FakeEngine(store), FakeProbes(store)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>index</html>")
    (web / "app.js").write_text("console.log(1)")
    (web / "logo.png").write_bytes(b"\x89PNG")
    a = create_app(Config(db_path=str(tmp_path / "api.db"), web_dir=web), store=store, bus=bus,
                   engine=eng, probes=pr, watcher=False, driver=False, mcp=False)
    a.fake_engine, a.fake_probes = eng, pr
    return a


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def err(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"error"} and body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    return body["error"]["message"]


def new_agent(client, name="Alpha", **kw):
    r = client.post("/api/v1/agents", json={"name": name, "runtime": "claude_code", **kw})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ state
def test_state(client, tmp_path):
    a = new_agent(client)
    s = client.get("/api/v1/state").json()
    assert set(s) == {"version", "demo_seeded", "agents", "edges", "runtimes", "servers", "settings", "hardware"}
    assert s["demo_seeded"] is False and s["agents"][0]["id"] == a["id"]
    assert len(s["servers"]) == 3 and s["runtimes"][0]["id"] == "demo" and s["hardware"] is None
    assert s["settings"]["max_depth"] == 3


def test_lifespan_starts_and_stops_modules(app):
    with TestClient(app) as c:
        assert app.fake_engine.started and app.fake_probes.started
        c.get("/api/v1/state")
    assert app.fake_engine.stopped and app.fake_probes.stopped
    assert app.fake_engine.refreshed == 1


# ------------------------------------------------------------------ agents
def test_agents_crud(client, tmp_path):
    a = new_agent(client, role="r", cwd=str(tmp_path))
    assert a["cwd"] == str(tmp_path) and a["live"]["status"] == "idle"
    assert client.get("/api/v1/agents").json()["agents"][0]["id"] == a["id"]
    p = client.patch(f"/api/v1/agents/{a['id']}", json={"x": 3, "name": "Beta"}).json()
    assert p["x"] == 3 and p["name"] == "Beta"
    assert client.delete(f"/api/v1/agents/{a['id']}").json() == {"ok": True}
    err(client.delete(f"/api/v1/agents/{a['id']}"), 404, "unknown_agent")
    err(client.patch("/api/v1/agents/nope", json={"x": 1}), 404, "unknown_agent")


def test_agents_errors(client):
    new_agent(client, "Alpha")
    err(client.post("/api/v1/agents", json={"name": "Alpha", "runtime": "demo"}), 409, "name_taken")
    err(client.post("/api/v1/agents", json={"runtime": "demo"}), 422, "validation")
    err(client.post("/api/v1/agents", json={"name": "x", "runtime": "nope"}), 422, "validation")
    err(client.post("/api/v1/agents", json={"name": "x", "runtime": "demo", "cwd": "/no/such/dir/xyz"}), 422, "bad_cwd")
    err(client.post("/api/v1/agents", content=b"not json"), 422, "validation")
    err(client.post("/api/v1/agents", json=[1]), 422, "validation")
    a = new_agent(client, "Zed")
    err(client.patch(f"/api/v1/agents/{a['id']}", json={"cwd": "/no/such/dir/xyz"}), 422, "bad_cwd")
    assert client.patch(f"/api/v1/agents/{a['id']}", json={"cwd": None}).json()["cwd"] is None


def test_delete_agent_cancels_runs(client, app):
    a = new_agent(client)
    run = client.post("/api/v1/runs", json={"agent_id": a["id"], "task": "work"}).json()
    client.delete(f"/api/v1/agents/{a['id']}")
    assert app.fake_engine.cancelled == [run["id"]]


# ------------------------------------------------------------------ edges
def test_edges(client):
    a, b, c = new_agent(client, "A"), new_agent(client, "B"), new_agent(client, "C")
    e = client.post("/api/v1/edges", json={"supervisor_id": a["id"], "subordinate_id": b["id"]}).json()
    assert set(e) == {"id", "supervisor_id", "subordinate_id", "created_at"}
    assert client.get("/api/v1/edges").json()["edges"] == [e]
    post = lambda s, t: client.post("/api/v1/edges", json={"supervisor_id": s, "subordinate_id": t})  # noqa: E731
    err(post(a["id"], a["id"]), 409, "self_edge")
    err(post(a["id"], b["id"]), 409, "duplicate_edge")
    err(post(b["id"], a["id"]), 409, "cycle")
    err(post(a["id"], "nope"), 404, "unknown_agent")
    err(client.post("/api/v1/edges", json={"supervisor_id": a["id"]}), 422, "validation")
    assert post(b["id"], c["id"]).status_code == 200
    assert client.delete(f"/api/v1/edges/{e['id']}").json() == {"ok": True}
    err(client.delete(f"/api/v1/edges/{e['id']}"), 404, "not_found")


# ------------------------------------------------------------------ runs
def test_runs_flow(client, app):
    a = new_agent(client)
    r = client.post("/api/v1/runs", json={"agent_id": a["id"], "task": "  do it  "}).json()
    assert r["task"] == "do it" and r["status"] == "running" and r["agent_name"] == "Alpha"
    st = app.state.store
    child = st.create_run({"agent_id": a["id"], "task": "sub", "parent_run_id": r["id"], "status": "ok"})
    s1 = st.create_step({"run_id": r["id"], "kind": "message", "name": "assistant"})
    s2 = st.create_step({"run_id": child["id"], "kind": "tool", "name": "Read"})
    got = client.get(f"/api/v1/runs/{child['id']}").json()  # asking for a child returns the whole root tree
    assert got["run"]["id"] == child["id"]
    assert [x["id"] for x in got["tree"]] == [r["id"], child["id"]]
    assert {x["id"] for x in got["steps"]} == {s1["id"], s2["id"]}
    assert [x["id"] for x in client.get("/api/v1/runs").json()["runs"]] == [child["id"], r["id"]]
    assert [x["id"] for x in client.get("/api/v1/runs?root_only=true").json()["runs"]] == [r["id"]]
    assert [x["id"] for x in client.get(f"/api/v1/runs?status=ok&agent_id={a['id']}").json()["runs"]] == [child["id"]]
    assert client.get("/api/v1/runs?origin=external").json()["runs"] == []
    assert len(client.get("/api/v1/runs?limit=1").json()["runs"]) == 1
    err(client.get("/api/v1/runs?limit=0"), 422, "validation")
    err(client.get("/api/v1/runs?limit=abc"), 422, "validation")
    assert client.post(f"/api/v1/runs/{r['id']}/cancel").json() == {"ok": True}
    assert client.get(f"/api/v1/runs/{r['id']}").json()["run"]["status"] == "cancelled"
    err(client.get("/api/v1/runs/nope"), 404, "not_found")
    err(client.post("/api/v1/runs/nope/cancel"), 404, "not_found")


def test_run_errors(client, tmp_path):
    a = new_agent(client)
    err(client.post("/api/v1/runs", json={"agent_id": a["id"], "task": "  "}), 422, "empty_task")
    err(client.post("/api/v1/runs", json={"agent_id": a["id"]}), 422, "empty_task")
    err(client.post("/api/v1/runs", json={"agent_id": "nope", "task": "x"}), 404, "unknown_agent")
    err(client.post("/api/v1/runs", json={"task": "x"}), 422, "validation")
    err(client.post("/api/v1/runs", json={"agent_id": a["id"], "task": "x", "cwd": "/no/such/dir/xyz"}), 422, "bad_cwd")
    bad = new_agent(client, "broken")
    msg = err(client.post("/api/v1/runs", json={"agent_id": bad["id"], "task": "x"}), 409, "runtime_unavailable")
    assert msg == "Среда не найдена"
    ok = client.post("/api/v1/runs", json={"agent_id": a["id"], "task": "x", "cwd": str(tmp_path)})
    assert ok.json()["cwd"] == str(tmp_path)


def test_runs_without_engine(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as c:
        a = c.post("/api/v1/agents", json={"name": "A", "runtime": "demo"}).json()
        msg = err(c.post("/api/v1/runs", json={"agent_id": a["id"], "task": "x"}), 409, "runtime_unavailable")
        assert "недоступ" in msg
        err(c.post("/api/v1/runtimes/refresh"), 409, "runtime_unavailable")
        assert c.get("/api/v1/runtimes").json() == {"runtimes": []}
        err(c.post("/api/v1/servers/lmstudio/check"), 409, "runtime_unavailable")
        err(c.get("/mcp/agent/x"), 409, "runtime_unavailable")
        assert c.get("/api/v1/health").json()["ok"] is True


def test_overview_endpoint(client, app):
    a = new_agent(client)
    app.state.store.create_run({"agent_id": a["id"], "task": "t", "status": "ok",
                                "started_at": int(time.time() * 1000) - 5000,
                                "ended_at": int(time.time() * 1000), "tokens_in": 4})
    o = client.get("/api/v1/overview?window_s=600&buckets=10").json()
    assert o["window_s"] == 600 and len(o["series"]["t"]) == 10 and o["kpis"]["runs"] == 1
    assert o["kpis"]["tokens_in"] == 4 and o["by_agent"][0]["agent_name"] == "Alpha"
    assert client.get("/api/v1/overview").json()["window_s"] == 3600
    err(client.get("/api/v1/overview?buckets=0"), 422, "validation")
    err(client.get("/api/v1/overview?window_s=-1"), 422, "validation")


# ------------------------------------------------------------------ runtimes
def test_runtimes(client, app):
    assert client.get("/api/v1/runtimes").json()["runtimes"][0]["id"] == "demo"
    assert client.post("/api/v1/runtimes/refresh").json()["runtimes"][0]["available"] is True
    assert app.fake_engine.refreshed == 2  # one at startup, one explicit


# ------------------------------------------------------------------ servers
def test_servers(client):
    assert {s["id"] for s in client.get("/api/v1/servers").json()["servers"]} == {"lmstudio", "llamacpp", "anthropic"}
    s = client.post("/api/v1/servers", json={"name": "Mine", "kind": "openai_compat",
                                             "base_url": "http://127.0.0.1:5000"}).json()
    assert s["builtin"] is False
    p = client.patch(f"/api/v1/servers/{s['id']}", json={"enabled": False, "name": "M2"}).json()
    assert p["enabled"] is False and p["name"] == "M2"
    chk = client.post(f"/api/v1/servers/{s['id']}/check").json()
    assert chk["status"]["state"] == "up"
    assert client.delete(f"/api/v1/servers/{s['id']}").json() == {"ok": True}
    err(client.delete(f"/api/v1/servers/{s['id']}"), 404, "not_found")
    err(client.delete("/api/v1/servers/lmstudio"), 409, "builtin")
    err(client.patch("/api/v1/servers/nope", json={"enabled": True}), 404, "not_found")
    err(client.post("/api/v1/servers/nope/check"), 404, "not_found")
    err(client.post("/api/v1/servers", json={"name": "n", "kind": "x", "base_url": "http://a"}), 422, "validation")
    err(client.post("/api/v1/servers", json={"name": "n", "kind": "lmstudio", "base_url": "nonsense"}), 422, "validation")
    assert client.patch("/api/v1/servers/lmstudio", json={"enabled": False}).json()["enabled"] is False


# ------------------------------------------------------------------ hardware
def test_hardware(client, app):
    assert client.get("/api/v1/hardware").json() == {"latest": None, "samples": []}
    now = int(time.time() * 1000)
    for i in range(400):
        app.state.store.add_hardware_sample({"ts": now - (400 - i) * 1000, "cpu_percent": i, "cpu_count": 2,
                                             "load_avg": [0, 0, 0], "ram_used_bytes": 1, "ram_total_bytes": 2,
                                             "swap_used_bytes": 0, "swap_total_bytes": 0, "gpus": [],
                                             "processes": [{"pid": 1}]})
    h = client.get("/api/v1/hardware?window_s=600").json()
    assert h["latest"]["processes"] == [{"pid": 1}] and h["latest"]["cpu_percent"] == 399
    assert 100 < len(h["samples"]) <= 300 and all("processes" not in s for s in h["samples"])
    assert len(client.get("/api/v1/hardware?window_s=10").json()["samples"]) <= 11
    state = client.get("/api/v1/state").json()
    assert state["hardware"]["cpu_percent"] == 399


# ------------------------------------------------------------------ settings
def test_settings(client, tmp_path):
    s = client.get("/api/v1/settings").json()
    assert s["max_depth"] == 3
    p = client.patch("/api/v1/settings", json={"max_depth": 2, "default_cwd": str(tmp_path)}).json()
    assert p["max_depth"] == 2 and p["default_cwd"] == str(tmp_path)
    assert client.get("/api/v1/settings").json() == p
    err(client.patch("/api/v1/settings", json={"max_depth": "deep"}), 422, "validation")
    err(client.patch("/api/v1/settings", json={"default_cwd": "/no/such/dir/xyz"}), 422, "bad_cwd")


# ------------------------------------------------------------------ fs/dirs
def test_fs_dirs(client, tmp_path):
    tmp_path = tmp_path / "root"
    tmp_path.mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")
    (tmp_path / "beta" / "inner").mkdir()
    r = client.get("/api/v1/fs/dirs", params={"path": str(tmp_path)}).json()
    assert r["path"] == str(tmp_path.resolve()) and r["parent"] == str(tmp_path.resolve().parent)
    assert [d["name"] for d in r["dirs"]] == ["Alpha", "beta"]
    assert r["dirs"][1]["path"] == str(tmp_path.resolve() / "beta")
    # path is resolved: .. segments and symlinks collapse
    r2 = client.get("/api/v1/fs/dirs", params={"path": str(tmp_path / "beta" / ".." / "Alpha")}).json()
    assert r2["path"] == str(tmp_path.resolve() / "Alpha") and r2["dirs"] == []
    assert client.get("/api/v1/fs/dirs", params={"path": "/"}).json()["parent"] is None
    home = client.get("/api/v1/fs/dirs").json()
    assert home["path"] == str(__import__("pathlib").Path.home().resolve())
    err(client.get("/api/v1/fs/dirs", params={"path": str(tmp_path / "file.txt")}), 422, "bad_cwd")
    err(client.get("/api/v1/fs/dirs", params={"path": str(tmp_path / "missing")}), 422, "bad_cwd")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_fs_dirs_permissions(client, tmp_path):
    tmp_path = tmp_path / "root"
    tmp_path.mkdir()
    locked = tmp_path / "locked"
    locked.mkdir()
    (tmp_path / "ok").mkdir()
    locked.chmod(0)
    try:
        r = client.get("/api/v1/fs/dirs", params={"path": str(tmp_path)}).json()
        assert [d["name"] for d in r["dirs"]] == ["ok"]
        err(client.get("/api/v1/fs/dirs", params={"path": str(locked)}), 422, "bad_cwd")
    finally:
        locked.chmod(0o755)


def test_fs_dirs_survives_oserror(client, tmp_path, monkeypatch):
    tmp_path = tmp_path / "root"
    tmp_path.mkdir()
    (tmp_path / "ok").mkdir()
    real = os.access

    def flaky(path, mode):
        if str(path).endswith("ok"):
            raise PermissionError("denied")
        return real(path, mode)
    # an unreadable entry must not break the listing
    monkeypatch.setattr(os, "access", lambda p, m: False if str(p).endswith("ok") else real(p, m))
    r = client.get("/api/v1/fs/dirs", params={"path": str(tmp_path)}).json()
    assert r["dirs"] == []


# ------------------------------------------------------------------ connect
def test_connect(client):
    c = client.get("/api/v1/connect").json()["mcp"]
    base = "http://testserver"
    assert c["agent_url_template"] == f"{base}/mcp/agent/{{agent_id}}"
    assert c["claude_command_template"] == f"claude mcp add --transport http orkestr {base}/mcp/agent/{{agent_id}}"
    assert json.loads(c["opencode_snippet_template"]) == {
        "mcp": {"orkestr": {"type": "remote", "url": f"{base}/mcp/agent/{{agent_id}}", "enabled": True}}}
    # the URL follows the Host the client used
    c2 = client.get("/api/v1/connect", headers={"host": "127.0.0.1:8787"}).json()["mcp"]
    assert c2["agent_url_template"] == "http://127.0.0.1:8787/mcp/agent/{agent_id}"


# ------------------------------------------------------------------ demo
def test_demo_endpoints(client, app, monkeypatch):
    import orkestr
    calls = []
    fake = types.ModuleType("orkestr.demo")

    def seed_demo(store):
        calls.append("seed")
        a = store.create_agent({"name": "Демо-1", "runtime": "demo"})
        b = store.create_agent({"name": "Демо-2", "runtime": "demo"})
        e = store.create_edge(a["id"], b["id"])
        return {"agents": [a, b], "edges": [e]}

    async def clear_demo(store, engine):
        calls.append("clear")
        store.delete_demo_data()
    fake.seed_demo, fake.clear_demo = seed_demo, clear_demo
    monkeypatch.setitem(sys.modules, "orkestr.demo", fake)
    monkeypatch.setattr(orkestr, "demo", fake, raising=False)
    r = client.post("/api/v1/demo/seed").json()
    assert len(r["agents"]) == 2 and len(r["edges"]) == 1
    assert client.get("/api/v1/state").json()["demo_seeded"] is True
    assert client.post("/api/v1/demo/clear").json() == {"ok": True}
    assert calls == ["seed", "clear"]
    s = client.get("/api/v1/state").json()
    assert s["demo_seeded"] is False and s["agents"] == [] and s["edges"] == []


# ------------------------------------------------------------------ errors / static
def test_unknown_api_path_and_method(client):
    err(client.get("/api/v1/nope"), 404, "not_found")
    assert client.put("/api/v1/agents").status_code == 405


def test_static_files(client):
    r = client.get("/")
    assert r.status_code == 200 and "index" in r.text and r.headers["cache-control"] == "no-cache"
    r = client.get("/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert client.get("/some/spa/route").text == "<html>index</html>"
    assert client.get("/logo.png").status_code == 200


# ------------------------------------------------------------------ SSE (real server)
class LiveServer:
    def __init__(self, app) -> None:
        import uvicorn
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    timeout_graceful_shutdown=2))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        assert self.server.started
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)
        assert not self.thread.is_alive(), "server did not shut down"


def read_events(resp, want: int, timeout: float = 5.0):
    events, cur = [], {}
    deadline = time.time() + timeout
    for line in resp.iter_lines():
        if line.startswith("event: "):
            cur["event"] = line[7:]
        elif line.startswith("data: "):
            cur["data"] = json.loads(line[6:])
        elif line == "" and cur:
            events.append(cur)
            cur = {}
            if len(events) >= want or time.time() > deadline:
                break
    return events


def test_sse_hello_and_agent_event(app):
    with LiveServer(app) as base:
        with httpx.Client(base_url=base, timeout=10) as c:
            with c.stream("GET", "/api/v1/stream") as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                assert resp.headers["cache-control"] == "no-cache"
                assert resp.headers["x-accel-buffering"] == "no"
                lines = resp.iter_lines()
                first = [next(lines) for _ in range(3)]
                assert first[0] == "event: hello"
                hello = json.loads(first[1][6:])
                assert hello["version"] and hello["now"] > 0
                threading.Timer(0.2, lambda: httpx.post(
                    f"{base}/api/v1/agents", json={"name": "Streamed", "runtime": "demo"})).start()
                got = None
                deadline = time.time() + 5
                typ = None
                for line in lines:
                    if line.startswith("event: "):
                        typ = line[7:]
                    elif line.startswith("data: ") and typ == "agent":
                        got = json.loads(line[6:])
                        break
                    if time.time() > deadline:
                        break
                assert got and got["name"] == "Streamed" and got["live"]["status"] == "idle"


def test_sse_ping_and_clean_shutdown(app, monkeypatch):
    import orkestr.api as api
    monkeypatch.setattr(api, "PING_INTERVAL_S", 0.3)
    server = LiveServer(app)
    with server as base:
        with httpx.Client(base_url=base, timeout=10) as c:
            with c.stream("GET", "/api/v1/stream") as resp:
                evs = read_events(resp, 2)
                assert [e["event"] for e in evs] == ["hello", "ping"] and evs[1]["data"]["now"] > 0
                assert app.state.bus.subscriber_count == 1
                # leave the stream open: shutdown must still finish quickly (checked in __exit__)
                t0 = time.time()
                app.state.begin_shutdown()
        assert time.time() - t0 < 3
    assert app.state.bus.subscriber_count == 0


def test_sse_subscriber_removed_on_disconnect(app):
    with LiveServer(app) as base:
        with httpx.Client(base_url=base, timeout=10) as c:
            with c.stream("GET", "/api/v1/stream") as resp:
                read_events(resp, 1)
                assert app.state.bus.subscriber_count == 1
        for _ in range(50):
            if app.state.bus.subscriber_count == 0:
                break
            time.sleep(0.1)
        # a disconnect is only noticed when the server next writes: trigger an event
        httpx.post(f"{base}/api/v1/agents", json={"name": "Wake", "runtime": "demo"})
        for _ in range(50):
            if app.state.bus.subscriber_count == 0:
                break
            time.sleep(0.1)
        assert app.state.bus.subscriber_count == 0
