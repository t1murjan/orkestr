"""SqliteStore: CRUD, DAG rules, live status, overview math, retention, hardware."""
from __future__ import annotations

import asyncio
import time

import pytest

from orkestr.bus import EventBus
from orkestr.interfaces import StoreError
from orkestr.store import SqliteStore, now_ms


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, type_: str, data: dict) -> None:
        self.events.append((type_, data))

    def of(self, type_: str) -> list[dict]:
        return [d for t, d in self.events if t == type_]


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def store(tmp_path, rec) -> SqliteStore:
    s = SqliteStore(str(tmp_path / "t.db"), rec)
    yield s
    s.close()


def mk(store: SqliteStore, name: str, **kw):
    return store.create_agent({"name": name, "runtime": "claude_code", **kw})


# ---------------------------------------------------------------- settings/seed
def test_seed_defaults(store):
    s = store.get_settings()
    assert s["max_depth"] == 3 and s["max_concurrent_runs"] == 4
    assert s["delegate_wait_s"] == 120 and s["run_timeout_s"] == 1800
    import os
    assert s["default_cwd"] == os.getcwd()
    assert s["watch_external"] is True and s["demo_auto"] is False
    servers = {x["id"]: x for x in store.list_servers()}
    assert set(servers) == {"lmstudio", "llamacpp", "anthropic"}
    assert all(x["builtin"] and x["enabled"] and x["status"] is None for x in servers.values())
    assert servers["anthropic"]["base_url"] == "https://api.anthropic.com"
    assert servers["anthropic"]["location"] == "cloud" and servers["lmstudio"]["location"] == "local"


def test_seed_is_idempotent_and_persistent(tmp_path, rec):
    p = str(tmp_path / "x.db")
    s1 = SqliteStore(p, rec)
    s1.update_settings({"max_depth": 5})
    s1.update_server("lmstudio", {"enabled": False})
    a = mk(s1, "A")
    s1.close()
    s2 = SqliteStore(p, rec)
    assert s2.get_settings()["max_depth"] == 5
    assert len(s2.list_servers()) == 3
    assert s2.get_server("lmstudio")["enabled"] is False
    assert s2.get_agent(a["id"])["name"] == "A"
    s2.close()


def test_update_settings_validation_and_event(store, rec):
    out = store.update_settings({"max_depth": 2, "demo_auto": True, "unknown": 1})
    assert out["max_depth"] == 2 and out["demo_auto"] is True and "unknown" not in out
    assert rec.of("settings")[-1]["max_depth"] == 2
    for bad in ({"max_depth": "x"}, {"max_depth": True}, {"demo_auto": 1}, {"max_concurrent_runs": 0},
                {"default_cwd": 5}):
        with pytest.raises(StoreError) as e:
            store.update_settings(bad)
        assert e.value.code == "validation"


# ----------------------------------------------------------------------- agents
def test_agent_crud_and_events(store, rec):
    a = mk(store, "  Alpha ", role="r", x=10, y=20.5, color="b")
    assert a["name"] == "Alpha" and a["permissions"] == "edit" and a["demo"] is False
    assert a["x"] == 10 and a["y"] == 20.5 and a["model"] is None and a["cwd"] is None
    assert a["live"]["status"] == "idle" and a["live"]["runs_total"] == 0
    assert rec.of("agent")[-1]["id"] == a["id"]
    b = store.update_agent(a["id"], {"model": "m1", "x": 5, "permissions": "full"})
    assert b["model"] == "m1" and b["x"] == 5 and b["permissions"] == "full"
    assert b["updated_at"] >= a["updated_at"]
    assert rec.of("agent")[-1]["model"] == "m1"
    assert store.get_agent_by_name("alpha")["id"] == a["id"]
    assert store.get_agent_by_name("nope") is None
    assert store.update_agent("zzz", {"x": 1}) is None
    assert store.delete_agent(a["id"]) is True
    assert rec.of("agent.deleted") == [{"id": a["id"]}]
    assert store.get_agent(a["id"]) is None and store.delete_agent(a["id"]) is False


def test_agent_validation(store):
    cases = [{"runtime": "claude_code"}, {"name": "", "runtime": "claude_code"},
             {"name": "x" * 41, "runtime": "claude_code"}, {"name": "x", "runtime": "gpt"},
             {"name": "x", "runtime": "demo", "permissions": "root"}, {"name": "x", "runtime": "demo", "color": "z"},
             {"name": "x", "runtime": "demo", "x": "1"}]
    for c in cases:
        with pytest.raises(StoreError) as e:
            store.create_agent(c)
        assert e.value.code == "validation", c
    assert store.create_agent({"name": "x" * 40, "runtime": "demo"})["demo"] is True


def test_name_unique_case_insensitive(store):
    a = mk(store, "Alpha")
    with pytest.raises(StoreError) as e:
        mk(store, "alpha")
    assert e.value.code == "name_taken"
    b = mk(store, "Beta")
    with pytest.raises(StoreError) as e:
        store.update_agent(b["id"], {"name": "ALPHA"})
    assert e.value.code == "name_taken"
    assert store.update_agent(a["id"], {"name": "ALPHA"})["name"] == "ALPHA"  # own name is fine


# ------------------------------------------------------------------------ edges
def test_edge_rules(store, rec):
    a, b, c = (mk(store, n) for n in "ABC")
    e = store.create_edge(a["id"], b["id"])
    assert rec.of("edge")[-1] == e
    with pytest.raises(StoreError) as x:
        store.create_edge(a["id"], a["id"])
    assert x.value.code == "self_edge"
    with pytest.raises(StoreError) as x:
        store.create_edge(a["id"], b["id"])
    assert x.value.code == "duplicate_edge"
    with pytest.raises(StoreError) as x:
        store.create_edge(b["id"], a["id"])
    assert x.value.code == "cycle"
    store.create_edge(b["id"], c["id"])
    with pytest.raises(StoreError) as x:
        store.create_edge(c["id"], a["id"])
    assert x.value.code == "cycle"
    with pytest.raises(StoreError) as x:
        store.create_edge(a["id"], "missing")
    assert x.value.code == "unknown_agent"
    assert [s["name"] for s in store.subordinates_of(a["id"])] == ["B"]
    assert [s["name"] for s in store.supervisors_of(c["id"])] == ["B"]
    assert store.delete_edge(e["id"]) and not store.delete_edge(e["id"])
    assert rec.of("edge.deleted") == [{"id": e["id"]}]
    store.create_edge(c["id"], a["id"])  # legal now: chain C->A, B->C


def test_long_chain_cycle(store):
    agents = [mk(store, f"n{i}") for i in range(60)]
    for i in range(59):
        store.create_edge(agents[i]["id"], agents[i + 1]["id"])
    with pytest.raises(StoreError) as x:
        store.create_edge(agents[59]["id"], agents[0]["id"])
    assert x.value.code == "cycle"
    store.create_edge(agents[0]["id"], agents[59]["id"])  # shortcut is a legal DAG edge


def test_diamond_is_legal(store):
    top, l, r, bot = (mk(store, n) for n in ("top", "l", "r", "bot"))
    for s, t in ((top, l), (top, r), (l, bot), (r, bot)):
        store.create_edge(s["id"], t["id"])
    assert len(store.supervisors_of(bot["id"])) == 2
    assert len(store.subordinates_of(top["id"])) == 2
    with pytest.raises(StoreError):
        store.create_edge(bot["id"], top["id"])


def test_delete_agent_removes_edges(store, rec):
    a, b = mk(store, "A"), mk(store, "B")
    e = store.create_edge(a["id"], b["id"])
    store.delete_agent(b["id"])
    assert store.list_edges() == []
    assert rec.of("edge.deleted") == [{"id": e["id"]}]


# ------------------------------------------------------------------------- runs
def run(store, agent, **kw):
    return store.create_run({"agent_id": agent["id"], "task": "t", "status": "running", **kw})


def test_create_run_defaults_and_tree(store, rec):
    a = mk(store, "A", model="m")
    r = store.create_run({"agent_id": a["id"], "task": "do"})
    assert r["status"] == "queued" and r["origin"] == "user" and r["depth"] == 0
    assert r["root_run_id"] == r["id"] and r["parent_run_id"] is None
    assert r["agent_name"] == "A" and r["runtime"] == "claude_code" and r["model"] == "m"
    for k in ("tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens", "num_turns",
              "tool_calls", "delegations"):
        assert r[k] == 0
    assert r["cost_usd"] == 0 and r["started_at"] > 0 and r["ended_at"] is None and r["demo"] is False
    child = store.create_run({"agent_id": a["id"], "task": "sub", "parent_run_id": r["id"]})
    grand = store.create_run({"agent_id": a["id"], "task": "sub2", "parent_run_id": child["id"]})
    assert child["root_run_id"] == r["id"] and child["depth"] == 1 and child["origin"] == "delegation"
    assert grand["root_run_id"] == r["id"] and grand["depth"] == 2
    assert [x["id"] for x in store.run_tree(r["id"])] == [r["id"], child["id"], grand["id"]]
    assert rec.of("run")[-1]["id"] == grand["id"]
    with pytest.raises(StoreError):
        store.create_run({"task": "x", "parent_run_id": "nope"})
    with pytest.raises(StoreError):
        store.create_run({"task": "x", "status": "weird"})


def test_update_run_duration(store, rec):
    a = mk(store, "A")
    r = run(store, a, started_at=1000)
    u = store.update_run(r["id"], {"status": "ok", "ended_at": 4500, "result": "done", "tokens_in": 7})
    assert u["duration_ms"] == 3500 and u["tokens_in"] == 7 and u["result"] == "done"
    assert rec.of("run")[-1]["status"] == "ok"
    assert store.update_run("nope", {"status": "ok"}) is None
    ext = store.create_run({"task": "ext", "origin": "external", "runtime": "claude_code"})
    assert ext["agent_id"] is None


def test_list_runs_filters(store):
    a, b = mk(store, "A"), mk(store, "B")
    r1 = run(store, a, started_at=1000)
    r2 = run(store, b, started_at=2000, status="ok")
    r3 = store.create_run({"agent_id": a["id"], "task": "c", "status": "error", "started_at": 3000,
                           "parent_run_id": r1["id"]})
    ids = lambda **kw: [r["id"] for r in store.list_runs(**kw)]  # noqa: E731
    assert ids() == [r3["id"], r2["id"], r1["id"]]
    assert ids(status="ok,error") == [r3["id"], r2["id"]]
    assert ids(agent_id=a["id"]) == [r3["id"], r1["id"]]
    assert ids(root_only=True) == [r2["id"], r1["id"]]
    assert ids(origin="delegation") == [r3["id"]]
    assert ids(before=3000) == [r2["id"], r1["id"]]
    assert ids(limit=1) == [r3["id"]]


def test_retention_keeps_latest_roots(store):
    store.retention_root_runs = 5
    a = mk(store, "A")
    roots = []
    for i in range(8):
        r = store.create_run({"agent_id": a["id"], "task": str(i), "status": "ok", "started_at": 1000 + i,
                              "ended_at": 1001 + i})
        child = store.create_run({"agent_id": a["id"], "task": "c", "status": "ok", "parent_run_id": r["id"],
                                  "started_at": 1000 + i})
        store.create_step({"run_id": child["id"], "kind": "message", "name": "assistant"})
        roots.append((r, child))
    left = {r["id"] for r in store.list_runs(root_only=True, limit=100)}
    assert left == {r["id"] for r, _ in roots[3:]}
    for r, c in roots[:3]:
        assert store.get_run(r["id"]) is None and store.get_run(c["id"]) is None
        assert store.list_steps([c["id"]]) == []
    for r, c in roots[3:]:
        assert store.get_run(c["id"]) is not None


def test_retention_spares_active_roots(store):
    store.retention_root_runs = 2
    a = mk(store, "A")
    old = run(store, a, started_at=1)
    for i in range(4):
        store.create_run({"agent_id": a["id"], "task": "x", "status": "ok", "started_at": 100 + i})
    assert store.get_run(old["id"]) is not None


def test_orphaned_runs_closed_on_open(tmp_path, rec):
    p = str(tmp_path / "o.db")
    s = SqliteStore(p, rec)
    a = mk(s, "A")
    r = run(s, a)
    st = s.create_step({"run_id": r["id"], "kind": "tool", "name": "Read"})
    s.close()
    s2 = SqliteStore(p, rec)
    assert s2.get_run(r["id"])["status"] == "error" and s2.get_run(r["id"])["ended_at"] is not None
    assert s2.get_step(st["id"])["status"] == "error"
    assert s2.get_agent(a["id"])["live"]["status"] == "error"
    s2.close()


# ------------------------------------------------------------------------ steps
def test_steps(store, rec):
    a = mk(store, "A")
    r = run(store, a)
    s0 = store.create_step({"run_id": r["id"], "kind": "tool", "name": "Read", "title": "Читает a.py",
                            "input": {"path": "a.py", "big": "x" * 5000}, "started_at": 100})
    s1 = store.create_step({"run_id": r["id"], "kind": "message", "name": "assistant", "started_at": 100})
    assert (s0["seq"], s1["seq"]) == (0, 1)
    assert s0["status"] == "running" and s1["title"] == "assistant"
    assert len(s0["input"]["big"]) == 4001 and s0["input"]["path"] == "a.py"
    u = store.update_step(s0["id"], {"status": "ok", "ended_at": 250, "output": "y" * 25000})
    assert u["duration_ms"] == 150 and len(u["output"]) == 20001
    assert rec.of("step")[-1]["id"] == s0["id"]
    assert store.update_step("nope", {}) is None
    assert [s["id"] for s in store.list_steps([r["id"]])] == [s0["id"], s1["id"]]
    assert store.list_steps([]) == []
    with pytest.raises(StoreError):
        store.create_step({"run_id": r["id"], "kind": "bogus"})
    with pytest.raises(StoreError):
        store.create_step({"run_id": "nope", "kind": "tool"})


# ------------------------------------------------------------------- live status
def test_live_idle_working_delegating_error(store):
    a = mk(store, "A")
    live = lambda: store.get_agent(a["id"])["live"]  # noqa: E731
    assert live()["status"] == "idle"

    r = run(store, a, activity="Читает src/app.py", context_used=100, context_window=1000, model="m")
    lv = live()
    assert lv["status"] == "working" and lv["active_runs"] == 1 and lv["current_run_id"] == r["id"]
    assert lv["activity"] == "Читает src/app.py" and lv["context_used"] == 100
    assert lv["context_window"] == 1000 and lv["model"] == "m" and lv["runs_total"] == 1

    d = store.create_step({"run_id": r["id"], "kind": "delegation", "name": "delegate"})
    assert live()["status"] == "delegating"
    store.update_step(d["id"], {"status": "ok", "ended_at": now_ms()})
    assert live()["status"] == "working"

    r2 = run(store, a, started_at=r["started_at"] + 10, activity="Пишет")
    lv = live()
    assert lv["active_runs"] == 2 and lv["current_run_id"] == r2["id"] and lv["activity"] == "Пишет"

    store.update_run(r["id"], {"status": "error", "ended_at": now_ms(), "tokens_in": 5, "tokens_out": 6,
                              "cost_usd": 0.5})
    store.update_run(r2["id"], {"status": "ok", "ended_at": now_ms(), "tokens_in": 1, "cost_usd": 0.25,
                                "context_used": 7, "context_window": 70})
    lv = live()
    # last finished run is r2 (ok) -> idle
    assert lv["status"] == "idle" and lv["last_status"] == "ok" and lv["active_runs"] == 0
    assert lv["tokens_in"] == 6 and lv["tokens_out"] == 6 and lv["cost_usd"] == pytest.approx(0.75)
    assert lv["runs_total"] == 2 and lv["context_used"] == 7 and lv["current_run_id"] is None

    r3 = run(store, a, started_at=now_ms() + 5)
    store.update_run(r3["id"], {"status": "error", "ended_at": now_ms() + 10, "error": "boom"})
    lv = live()
    assert lv["status"] == "error" and lv["last_status"] == "error"
    r4 = run(store, a, started_at=now_ms() + 50)
    assert live()["status"] == "working"
    store.update_run(r4["id"], {"status": "cancelled", "ended_at": now_ms() + 60})
    assert live()["status"] == "idle" and live()["last_status"] == "cancelled"


def test_live_queued_is_not_active_and_in_agent_lists(store):
    a = mk(store, "A")
    store.create_run({"agent_id": a["id"], "task": "q"})
    lv = store.list_agents()[0]["live"]
    assert lv["status"] == "idle" and lv["active_runs"] == 0 and lv["runs_total"] == 1


def test_agent_event_on_live_change(store, rec):
    a = mk(store, "A")
    n = len(rec.of("agent"))
    r = run(store, a)
    ev = rec.of("agent")
    assert len(ev) > n and ev[-1]["live"]["status"] == "working"
    store.update_run(r["id"], {"status": "ok", "ended_at": now_ms()})
    assert rec.of("agent")[-1]["live"]["status"] == "idle"
    n = len(rec.of("agent"))
    store.update_run(r["id"], {"result": "x"})  # does not touch live
    assert len(rec.of("agent")) == n


async def test_agent_events_are_coalesced_and_final_state_delivered(tmp_path):
    rec = Recorder()
    s = SqliteStore(str(tmp_path / "c.db"), rec)
    a = mk(s, "A")
    r = s.create_run({"agent_id": a["id"], "task": "t", "status": "running"})
    base = len(rec.of("agent"))
    t0 = time.monotonic()
    i = 0
    while time.monotonic() - t0 < 1.0:
        i += 1
        s.update_run(r["id"], {"tokens_in": i, "activity": f"step {i}"})
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.4)
    got = rec.of("agent")[base:]
    assert 2 <= len(got) <= 7, len(got)  # <= ~4/s over ~1s (+ leading/trailing slack)
    assert got[-1]["live"]["tokens_in"] == i and got[-1]["live"]["activity"] == f"step {i}"
    s.close()


async def test_trailing_agent_event_is_delivered_after_burst(tmp_path):
    rec = Recorder()
    s = SqliteStore(str(tmp_path / "c.db"), rec)
    a = mk(s, "A")
    r = s.create_run({"agent_id": a["id"], "task": "t", "status": "running"})
    base = len(rec.of("agent"))
    s.update_run(r["id"], {"activity": "one"})
    s.update_run(r["id"], {"activity": "two"})
    s.update_run(r["id"], {"activity": "three"})
    assert len(rec.of("agent")) - base <= 1  # others are deferred
    await asyncio.sleep(0.5)
    assert rec.of("agent")[-1]["live"]["activity"] == "three"
    s.close()


async def test_pending_agent_timer_dropped_on_delete(tmp_path):
    rec = Recorder()
    s = SqliteStore(str(tmp_path / "c.db"), rec)
    a = mk(s, "A")
    r = s.create_run({"agent_id": a["id"], "task": "t", "status": "running"})
    s.update_run(r["id"], {"activity": "x"})
    s.delete_agent(a["id"])
    n = len(rec.of("agent"))
    await asyncio.sleep(0.4)
    assert len(rec.of("agent")) == n
    s.close()


# ---------------------------------------------------------------------- servers
def test_servers(store, rec):
    s = store.create_server({"name": "Мой", "kind": "openai_compat", "base_url": "http://localhost:9000/"})
    assert s["base_url"] == "http://localhost:9000" and s["builtin"] is False and s["enabled"] is True
    assert rec.of("server")[-1]["id"] == s["id"]
    u = store.update_server(s["id"], {"enabled": False, "name": "Другой"})
    assert u["enabled"] is False and u["name"] == "Другой"
    with pytest.raises(StoreError) as e:
        store.update_server(s["id"], {"base_url": "ftp://x"})
    assert e.value.code == "validation"
    with pytest.raises(StoreError):
        store.create_server({"name": "n", "kind": "weird", "base_url": "http://x"})
    ts = now_ms()
    st = store.save_server_status(s["id"], {"state": "up", "checked_at": ts, "latency_ms": 12,
                                            "error": None, "version": None, "models": [], "slots": [],
                                            "metrics": None})
    assert st["status"]["state"] == "up" and rec.of("server")[-1]["status"]["latency_ms"] == 12
    assert store.list_server_latency(s["id"], 0) == [{"ts": ts, "latency_ms": 12, "up": True}]
    assert store.save_server_status("nope", {"state": "up"}) is None
    assert store.delete_server(s["id"]) and not store.delete_server(s["id"])
    assert rec.of("server.deleted") == [{"id": s["id"]}]
    with pytest.raises(StoreError) as e:
        store.delete_server("lmstudio")
    assert e.value.code == "builtin"


# --------------------------------------------------------------------- hardware
def sample(ts: int) -> dict:
    return {"ts": ts, "cpu_percent": 1.0, "cpu_count": 4, "load_avg": [0, 0, 0], "ram_used_bytes": 1,
            "ram_total_bytes": 2, "swap_used_bytes": 0, "swap_total_bytes": 0, "gpus": [], "processes": []}


def test_hardware_downsample_and_prune(store, rec):
    now = now_ms()
    for i in range(1001):
        store.add_hardware_sample(sample(now - 1000 * (1000 - i)))
    assert rec.of("hardware")[-1]["ts"] == now
    assert len(store.list_hardware_samples(0, 5000)) == 1001
    pts = store.list_hardware_samples(0, 300)
    assert 100 < len(pts) <= 300
    assert pts[-1]["ts"] == now and pts[0]["ts"] == now - 1000 * 1000
    assert [p["ts"] for p in pts] == sorted(p["ts"] for p in pts)
    assert len(store.list_hardware_samples(now - 10_500, 300)) == 11
    assert store.list_hardware_samples(now + 10_000, 300) == []


def test_hardware_keeps_six_hours(tmp_path, rec):
    s = SqliteStore(str(tmp_path / "h.db"), rec)
    now = now_ms()
    s.add_hardware_sample(sample(now - 7 * 3600 * 1000))  # first insert triggers a prune pass
    s._last_hw_prune = 0.0
    s.add_hardware_sample(sample(now - 5 * 3600 * 1000))
    s._last_hw_prune = 0.0
    s.add_hardware_sample(sample(now))
    ts = [x["ts"] for x in s.list_hardware_samples(0, 100)]
    assert ts == [now - 5 * 3600 * 1000, now]
    s.close()


# --------------------------------------------------------------------- overview
def test_overview_hand_built_dataset(store):
    now = now_ms()
    W = 3600_000
    a, b = mk(store, "A", model="m1"), mk(store, "B", runtime="opencode", model="m2")

    def add(agent, ago, dur, status, **kw):
        started = now - ago
        d = {"agent_id": agent["id"] if agent else None, "task": "t", "status": status, "started_at": started,
             "ended_at": started + dur if status not in ("running",) else None, **kw}
        return store.create_run(d)

    add(a, 3100_000, 10_000, "ok", tokens_in=100, tokens_out=50, cost_usd=1.0, tool_calls=3, delegations=1)
    add(a, 2000_000, 20_000, "error", tokens_in=10, tokens_out=5, cost_usd=0.5, tool_calls=1)
    add(b, 1000_000, 30_000, "ok", tokens_in=200, tokens_out=100, cost_usd=2.0, model="m2", delegations=2)
    add(b, 500_000, 40_000, "cancelled", tokens_in=1, tokens_out=1, cost_usd=0.25, model="m2")
    add(a, 100_000, 0, "running", model="m1")                               # still running
    add(None, 200_000, 50_000, "ok", runtime="claude_code", model="ext", origin="external", tokens_in=7)
    # previous window
    add(a, W + 1000_000, 100_000, "ok", tokens_in=1000, tokens_out=500, cost_usd=9.0)
    add(b, W + 2000_000, 200_000, "error", tokens_in=2000, cost_usd=1.0, model="m2")
    # outside both windows
    add(a, 2 * W + 600_000, 5000, "ok", tokens_in=99999)

    o = store.overview(3600, 6)
    k = o["kpis"]
    assert o["window_s"] == 3600 and abs(o["now"] - now) < 5000
    assert k["runs"] == 6 and k["runs_ok"] == 3 and k["runs_error"] == 1 and k["active_runs"] == 1
    assert k["delegations"] == 3 and k["tool_calls"] == 4
    assert k["tokens_in"] == 318 and k["tokens_out"] == 156
    assert k["cost_usd"] == pytest.approx(3.75)
    # durations: 10,20,30,40,50 s (running one has none) -> avg 30 s, p95 = 50 s
    assert k["avg_run_ms"] == 30_000 and k["p95_run_ms"] == 50_000
    assert k["prev"] == {"runs": 2, "runs_error": 1, "tokens_in": 3000, "tokens_out": 500,
                         "cost_usd": 10.0, "avg_run_ms": 150_000}

    s = o["series"]
    assert all(len(v) == 6 for v in s.values())
    assert sum(s["runs"]) == 6 and sum(s["errors"]) == 1 and sum(s["tokens_in"]) == 318
    assert sum(s["cost_usd"]) == pytest.approx(3.75)
    assert s["t"][1] - s["t"][0] == 600_000
    # buckets are 600 s wide; offsets from the window start: 500, 1600, 2600, 3100, 3400, 3500 s
    assert s["runs"] == [1, 0, 1, 0, 1, 3]
    assert s["errors"] == [0, 0, 1, 0, 0, 0]
    assert s["active"][-1] >= 1  # the running run is active at the final bucket end

    by_agent = {e["agent_name"]: e for e in o["by_agent"]}
    assert by_agent["A"]["runs"] == 3 and by_agent["A"]["errors"] == 1 and by_agent["A"]["tokens_in"] == 110
    assert by_agent["A"]["avg_run_ms"] == 15_000  # (10s + 20s) / 2, running one excluded
    assert by_agent["B"]["runs"] == 2 and by_agent["B"]["cost_usd"] == pytest.approx(2.25)
    assert o["by_agent"][0]["runs"] >= o["by_agent"][-1]["runs"]
    ext = [e for e in o["by_agent"] if e["agent_id"] is None]
    assert len(ext) == 1 and ext[0]["runs"] == 1

    models = {(e["model"], e["runtime"]): e for e in o["by_model"]}
    assert models[("m1", "claude_code")]["runs"] == 3
    assert models[("m2", "opencode")]["runs"] == 2 and models[("ext", "claude_code")]["runs"] == 1
    rt = {e["runtime"]: e for e in o["by_runtime"]}
    assert rt["claude_code"]["runs"] == 4 and rt["opencode"]["runs"] == 2
    assert rt["claude_code"]["tokens_in"] == 117 and rt["opencode"]["errors"] == 0


def test_overview_empty(store):
    o = store.overview(60, 4)
    assert o["kpis"]["runs"] == 0 and o["kpis"]["avg_run_ms"] is None and o["kpis"]["p95_run_ms"] is None
    assert o["series"]["runs"] == [0, 0, 0, 0] and o["by_agent"] == [] and o["by_model"] == []


def test_overview_active_series(store):
    now = now_ms()
    a = mk(store, "A")
    # started before the window, still running: active in every bucket
    store.create_run({"agent_id": a["id"], "task": "t", "status": "running", "started_at": now - 7200_000})
    # finished in the middle of the window
    store.create_run({"agent_id": a["id"], "task": "t", "status": "ok", "started_at": now - 3000_000,
                      "ended_at": now - 1500_000})
    o = store.overview(3600, 4)
    assert o["series"]["active"] == [2, 2, 1, 1]
    assert o["kpis"]["runs"] == 1


def test_demo_cleanup(store, rec):
    d = store.create_agent({"name": "D", "runtime": "demo"})
    n = mk(store, "N")
    r = store.create_run({"agent_id": d["id"], "task": "t", "status": "ok"})
    keep = store.create_run({"agent_id": n["id"], "task": "t", "status": "ok"})
    assert r["demo"] is True and keep["demo"] is False and store.has_demo_agents()
    store.delete_demo_data()
    assert [a["name"] for a in store.list_agents()] == ["N"]
    assert store.get_run(r["id"]) is None and store.get_run(keep["id"]) is not None
    assert rec.of("demo.cleared") == [{}]


def test_bus_failure_does_not_break_mutation(tmp_path):
    class Boom:
        def publish(self, *a):
            raise RuntimeError("x")
    s = SqliteStore(str(tmp_path / "b.db"), Boom())
    assert mk(s, "A")["name"] == "A"
    s.close()
