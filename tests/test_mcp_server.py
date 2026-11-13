"""MCP server tests over real HTTP (uvicorn on a free port).

Interop: the official `mcp` 2.x client (`mcp.Client`, both `auto` and `legacy` negotiation) talks to
`/mcp/run/{token}` and `/mcp/agent/{id}`; raw httpx covers protocol edge cases. The real Claude Code
CLI check is in tests/test_mcp_claude_cli.py (skipped when `claude` is missing or not logged in).
"""
from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
import pytest_asyncio
import uvicorn
from mcp import Client
from starlette.applications import Starlette
from starlette.routing import Mount

from orkestr.mcp_server import build_mcp_app
from orkestr.runtimes.base import RuntimeEvent
from tests.fakes import ScriptedAdapter, make_env, wait_until



def E(type_, **data):
    return RuntimeEvent(type_, data)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Rig:
    """Parent ("Босс") that idles until released and lets the test play the CLI's tool events."""

    def __init__(self) -> None:
        self.env = None
        self.url = ""
        self.events: asyncio.Queue = asyncio.Queue()
        self.child_gate = asyncio.Event()
        self.child_gate.set()
        self.child_started = 0

    async def script(self, spec):
        name = spec.agent["name"]
        if name == "Босс" or name == "Лид":
            while True:
                ev = await self.events.get()
                if ev is None:
                    break
                yield ev
            yield E("result", text="босс закончил", is_error=False, cost_usd=None, num_turns=1)
        else:
            self.child_started += 1
            yield E("text", text=f"{name} начал", delta=False)
            await self.child_gate.wait()
            yield E("result", text=f"готово от {name}: {spec.task}", is_error=False, cost_usd=None, num_turns=1)

    def boss_url(self, run_id: str) -> str:
        spec = next(s for s in self.env.adapter.specs if s.run_id == run_id)
        assert spec.mcp_url
        return spec.mcp_url


@pytest_asyncio.fixture(params=["fake", "sqlite"])
async def rig(request, tmp_path):
    r = Rig()
    port = free_port()
    env = await make_env(request.param, tmp_path, r.script)
    env.engine.base_url = f"http://127.0.0.1:{port}"
    r.env = env
    app = Starlette(routes=[Mount("/mcp", app=build_mcp_app(env.engine, env.store))])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off",
                                           timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve())
    await wait_until(lambda: server.started, timeout=10)
    r.base = f"http://127.0.0.1:{port}"
    yield r
    r.child_gate.set()
    r.events.put_nowait(None)
    await env.engine.stop()
    server.should_exit = True
    await asyncio.wait_for(task, 10)
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not leftover, leftover


async def start_boss(rig: Rig, task="задача"):
    env = rig.env
    boss = env.store.get_agent_by_name("Босс") or env.agent("Босс", role="руководит")
    dev = env.store.get_agent_by_name("Дев") or env.agent("Дев", role="пишет код")
    if not env.store.subordinates_of(boss["id"]):
        env.link(boss, dev)
    run = await env.engine.start_run(boss["id"], task)
    await wait_until(lambda: any(s.run_id == run["id"] for s in env.adapter.specs))
    return run, rig.boss_url(run["id"])


def text_of(res) -> str:
    return "".join(c.text for c in res.content if getattr(c, "type", "") == "text")


# ------------------------------------------------------------------ raw protocol
async def rpc(http, url, method, params=None, id_=1, headers=None):
    body = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    return await http.post(url, json=body, headers=headers or {})


async def test_protocol_edges(rig):
    run, url = await start_boss(rig)
    async with httpx.AsyncClient() as http:
        for asked, expect in [("2025-06-18", "2025-06-18"), ("2024-11-05", "2024-11-05"),
                              ("2025-03-26", "2025-03-26"), ("2099-01-01", "2025-11-25"),
                              ("2025-11-25", "2025-11-25")]:
            r = await rpc(http, url, "initialize", {"protocolVersion": asked, "capabilities": {},
                                                    "clientInfo": {"name": "t", "version": "1"}})
            assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
            res = r.json()["result"]
            assert res["protocolVersion"] == expect and res["serverInfo"]["name"] == "orkestr"
            assert "tools" in res["capabilities"]
        n = await rpc(http, url, "notifications/initialized", id_=None)
        assert n.status_code == 202 and n.content == b""
        assert (await rpc(http, url, "ping")).json()["result"] == {}
        tools = (await rpc(http, url, "tools/list")).json()["result"]["tools"]
        assert [t["name"] for t in tools] == ["list_subordinates", "delegate", "check_task"]
        assert all(t["inputSchema"]["type"] == "object" for t in tools)
        assert tools[1]["inputSchema"]["required"] == ["agent", "task"]
        assert (await rpc(http, url, "tools/call", {"name": "nope"})).json()["error"]["code"] == -32602
        assert (await rpc(http, url, "resources/list")).json()["error"]["code"] == -32601
        assert (await rpc(http, url, "server/discover", {})).json()["error"]["code"] == -32601
        bad = await rpc(http, url, "ping", headers={"mcp-protocol-version": "2026-07-28"})
        assert bad.status_code == 400 and bad.json()["error"]["code"] == -32022
        assert "2025-06-18" in bad.json()["error"]["data"]["supported"]
        ok = await rpc(http, url, "ping", headers={"mcp-protocol-version": "2025-06-18"})
        assert ok.status_code == 200
        assert (await http.post(url, content=b"{nope")).status_code == 400
        assert (await http.get(url)).status_code == 405
        assert (await http.delete(url)).status_code == 405
        assert (await http.post(url, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                           {"jsonrpc": "2.0", "method": "notifications/initialized"}])).json() \
            == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
        assert (await http.post(url, json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
                                headers={"origin": "http://evil.example"})).status_code == 403
        assert (await http.post(url, json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
                                headers={"origin": "http://localhost:8787"})).status_code == 200
        wrong = rig.base + "/mcp/run/not-a-token"
        assert (await rpc(http, wrong, "ping")).status_code == 404
        assert (await rpc(http, rig.base + "/mcp/agent/missing", "ping")).status_code == 404
        assert (await rpc(http, rig.base + "/mcp/other", "ping")).status_code == 404
        # argument validation is a tool error, not a transport error
        r = (await rpc(http, url, "tools/call", {"name": "delegate", "arguments": {"agent": 1}})).json()["result"]
        assert r["isError"] is True


# ------------------------------------------------------------------ SDK client, full delegation
@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("start_first", [True, False])
async def test_delegation_end_to_end_with_sdk_client(rig, mode, start_first):
    env = rig.env
    run, url = await start_boss(rig, "большая задача")
    async with Client(url, mode=mode) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools.tools] == ["list_subordinates", "delegate", "check_task"]
        subs = json.loads(text_of(await client.call_tool("list_subordinates", {})))
        assert subs == [{"name": "Дев", "role": "пишет код", "runtime": "demo", "model": None, "status": "idle"}]

        start = E("tool_start", tool_id="d1", name="mcp__orkestr__delegate",
                  input={"agent": "Дев", "task": "сделай X"}, mcp={"server": "orkestr", "tool": "delegate"})
        if start_first:
            rig.events.put_nowait(start)
            await wait_until(lambda: any(s["kind"] == "delegation" for s in env.steps(run["id"])))
        else:
            async def late():
                await asyncio.sleep(0.2)
                rig.events.put_nowait(start)
            lt = asyncio.create_task(late())
        res = await client.call_tool("delegate", {"agent": "Дев", "task": "сделай X"})
        if not start_first:
            await lt
        assert not res.is_error and text_of(res) == "готово от Дев: сделай X"
        rig.events.put_nowait(E("tool_end", tool_id="d1", output=text_of(res), is_error=False))
    await wait_until(lambda: any(s["kind"] == "delegation" and s["status"] == "ok" for s in env.steps(run["id"])))
    (step,) = [s for s in env.steps(run["id"]) if s["kind"] == "delegation"]
    child = env.store.get_run(step["child_run_id"])
    assert child["status"] == "ok" and child["origin"] == "delegation" and child["agent_name"] == "Дев"
    assert child["parent_run_id"] == run["id"] and child["parent_step_id"] == step["id"] and child["depth"] == 1
    assert env.store.get_run(run["id"])["delegations"] == 1
    assert step["title"] == "Поручает задачу: Дев" and step["output"] == "готово от Дев: сделай X"
    rig.events.put_nowait(None)
    assert (await env.engine.wait_run(run["id"], 5))["status"] == "ok"


async def test_long_task_returns_task_id_then_check_task(rig):
    env = rig.env
    real = env.store.get_settings
    env.store.get_settings = lambda: {**real(), "delegate_wait_s": 0.3}
    rig.child_gate.clear()
    run, url = await start_boss(rig)
    async with Client(url) as client:
        res = await client.call_tool("delegate", {"agent": "Дев", "task": "долго"})
        info = json.loads(text_of(res))
        assert not res.is_error and info["status"] == "running" and "check_task" in info["message"]
        tid = info["task_id"]
        child = env.store.get_run(tid)
        assert child["parent_run_id"] == run["id"] and child["status"] in ("running", "queued")
        again = json.loads(text_of(await client.call_tool("check_task", {"task_id": tid, "wait_s": 0})))
        assert again["status"] == "running"
        asyncio.get_running_loop().call_later(0.3, rig.child_gate.set)
        done = await client.call_tool("check_task", {"task_id": tid, "wait_s": 10})
        assert not done.is_error and text_of(done) == "готово от Дев: долго"
        # somebody else's / invented ids are refused
        bad = await client.call_tool("check_task", {"task_id": "zzz"})
        assert bad.is_error and "не найдена" in text_of(bad)
    # no dangling pending links for a delegate nobody announced
    ctx = env.engine._ctx[run["id"]]
    assert [p.run_id for p in ctx.pending_children] == [tid]
    rig.events.put_nowait(None)
    await env.engine.wait_run(run["id"], 5)


async def test_refuses_non_subordinate_and_validates(rig):
    env = rig.env
    env.agent("Чужой")
    run, url = await start_boss(rig)
    async with Client(url) as client:
        res = await client.call_tool("delegate", {"agent": "Чужой", "task": "x"})
        assert res.is_error and "не ваш подчинённый" in text_of(res) and "«Дев»" in text_of(res)
        res = await client.call_tool("delegate", {"agent": "Дев", "task": "  "})
        assert res.is_error
        res = await client.call_tool("delegate", {"agent": "дев", "task": "регистр не важен"})
        assert not res.is_error and text_of(res).startswith("готово от Дев")
    assert [r["agent_name"] for r in env.store.run_tree(run["id"])].count("Чужой") == 0
    rig.events.put_nowait(None)
    await env.engine.wait_run(run["id"], 5)


async def test_link_added_mid_run_is_visible(rig):
    env = rig.env
    run, url = await start_boss(rig)
    ops = env.agent("Опс", role="администрирует")
    async with Client(url) as client:
        names = lambda r: {s["name"] for s in json.loads(text_of(r))}  # noqa: E731
        assert names(await client.call_tool("list_subordinates", {})) == {"Дев"}
        boss = env.store.get_agent_by_name("Босс")
        edge = env.link(boss, ops)
        assert names(await client.call_tool("list_subordinates", {})) == {"Дев", "Опс"}
        res = await client.call_tool("delegate", {"agent": "Опс", "task": "деплой"})
        assert text_of(res) == "готово от Опс: деплой"
        env.store.delete_edge(edge["id"])
        res = await client.call_tool("delegate", {"agent": "Опс", "task": "ещё раз"})
        assert res.is_error
    rig.events.put_nowait(None)
    await env.engine.wait_run(run["id"], 5)


async def test_depth_limit_via_mcp(rig):
    env = rig.env
    env.store.update_settings({"max_depth": 0})
    run, url = await start_boss(rig)
    async with Client(url) as client:
        res = await client.call_tool("delegate", {"agent": "Дев", "task": "x"})
        assert res.is_error and "глубина" in text_of(res)
    rig.events.put_nowait(None)
    await env.engine.wait_run(run["id"], 5)


async def test_child_error_is_a_tool_error(rig):
    env = rig.env

    async def script(spec):
        if spec.agent["name"] == "Дев":
            yield E("error", message="дев сломался", fatal=True)
        else:
            async for ev in rig.script(spec):
                yield ev

    env.adapter.script = script
    run, url = await start_boss(rig)
    async with Client(url) as client:
        res = await client.call_tool("delegate", {"agent": "Дев", "task": "x"})
        assert res.is_error and "дев сломался" in text_of(res)
    rig.events.put_nowait(None)
    await env.engine.wait_run(run["id"], 5)


async def test_agent_identity_endpoint(rig):
    env = rig.env
    boss = env.agent("Лид", role="внешний лид")
    dev = env.agent("Дев", role="пишет код")
    env.link(boss, dev)
    url = f"{rig.base}/mcp/agent/{boss['id']}"
    async with Client(url) as client:
        subs = json.loads(text_of(await client.call_tool("list_subordinates", {})))
        assert [s["name"] for s in subs] == ["Дев"]
        res = await client.call_tool("delegate", {"agent": "Дев", "task": "из терминала"})
        assert text_of(res) == "готово от Дев: из терминала"
        roots = env.store.list_runs(root_only=True)
        assert len(roots) == 1
        r = roots[0]
        assert r["origin"] == "delegation" and r["parent_run_id"] is None and r["depth"] == 0
        assert r["root_run_id"] == r["id"] and r["agent_name"] == "Дев"
        # long task by agent identity, check_task by the same identity only
        rig.child_gate.clear()
        env.store.get_settings  # noqa: B018
        real = env.store.get_settings
        env.store.get_settings = lambda: {**real(), "delegate_wait_s": 0.2}
        res = await client.call_tool("delegate", {"agent": "Дев", "task": "долго"})
        tid = json.loads(text_of(res))["task_id"]
        rig.child_gate.set()
        done = await client.call_tool("check_task", {"task_id": tid, "wait_s": 5})
        assert text_of(done) == "готово от Дев: долго"
    # a different identity cannot see the task
    other = env.agent("Другой")
    async with Client(f"{rig.base}/mcp/agent/{other['id']}") as c2:
        res = await c2.call_tool("check_task", {"task_id": tid})
        assert res.is_error


async def test_token_dies_with_run(rig):
    env = rig.env
    run, url = await start_boss(rig)
    rig.events.put_nowait(None)
    await env.engine.wait_run(run["id"], 5)
    async with httpx.AsyncClient() as http:
        r = await rpc(http, url, "tools/list")
        assert r.status_code == 404


async def test_cancelling_parent_during_delegation_kills_children(rig):
    env = rig.env
    rig.child_gate.clear()
    run, url = await start_boss(rig)
    results = {}

    async def call():
        async with httpx.AsyncClient(timeout=30) as http:
            try:
                r = await rpc(http, url, "tools/call", {"name": "delegate", "arguments": {"agent": "Дев", "task": "долго"}})
                results["r"] = r
            except httpx.HTTPError as e:
                results["err"] = e

    t = asyncio.create_task(call())
    await wait_until(lambda: len(env.store.run_tree(run["id"])) == 2)
    assert await env.engine.cancel_run(run["id"])
    await asyncio.wait_for(t, 10)
    tree = env.store.run_tree(run["id"])
    assert {r["status"] for r in tree} == {"cancelled"}
    assert "r" in results and results["r"].json()["result"]["isError"] is True     # told "cancelled", not hung
    assert not env.engine._slots and env.adapter.live == 0


async def test_client_disconnect_frees_parent_slot_but_keeps_child(rig):
    env = rig.env
    env.store.update_settings({"max_concurrent_runs": 2})
    rig.child_gate.clear()
    run, url = await start_boss(rig)
    ctx = env.engine._ctx[run["id"]]
    async with httpx.AsyncClient(timeout=30) as http:
        t = asyncio.create_task(rpc(http, url, "tools/call",
                                    {"name": "delegate", "arguments": {"agent": "Дев", "task": "долго"}}))
        await wait_until(lambda: ctx.blocked == 1)
        assert run["id"] not in env.engine._slots
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    await wait_until(lambda: ctx.blocked == 0, timeout=5)
    assert run["id"] in env.engine._slots                      # slot taken back
    child = next(r for r in env.store.run_tree(run["id"]) if r["id"] != run["id"])
    assert env.store.get_run(child["id"])["status"] == "running"
    rig.child_gate.set()
    await env.engine.wait_run(child["id"], 5)
    rig.events.put_nowait(None)
    await env.engine.wait_run(run["id"], 5)


async def test_parallel_delegations_same_agent_over_http(rig):
    env = rig.env
    run, url = await start_boss(rig)
    mcp = {"server": "orkestr", "tool": "delegate"}
    for i, task in enumerate(["альфа", "бета"]):
        rig.events.put_nowait(E("tool_start", tool_id=f"d{i}", name="mcp__orkestr__delegate",
                                input={"agent": "Дев", "task": task}, mcp=mcp))
    await wait_until(lambda: len([s for s in env.steps(run["id"]) if s["kind"] == "delegation"]) == 2)

    async def one(task):
        async with Client(url) as c:
            return text_of(await c.call_tool("delegate", {"agent": "Дев", "task": task}))

    out = await asyncio.gather(one("бета"), one("альфа"))      # arrive in the opposite order
    assert out == ["готово от Дев: бета", "готово от Дев: альфа"]
    steps = {s["input"]["task"]: s for s in env.steps(run["id"]) if s["kind"] == "delegation"}
    for task, s in steps.items():
        assert env.store.get_run(s["child_run_id"])["task"] == task
    rig.events.put_nowait(None)
    await env.engine.wait_run(run["id"], 5)
