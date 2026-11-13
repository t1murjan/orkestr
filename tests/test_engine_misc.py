"""activity phrases, DemoAdapter (standalone), demo seed/clear/driver, build_adapters."""
from __future__ import annotations

import asyncio

import pytest

from orkestr.activity import describe_tool
from orkestr.bus import EventBus
from orkestr.demo import DEMO_TASKS, LEAD_NAME, DemoDriver, clear_demo, seed_demo
from orkestr.engine import RunEngine
from orkestr.runtimes import build_adapters
from orkestr.runtimes.base import RunSpec
from orkestr.runtimes.demo import DemoAdapter
from tests.fakes import FakeStore, ScriptedAdapter, make_env, make_store, wait_until



# ------------------------------------------------------------------ activity
@pytest.mark.parametrize("name,inp,mcp,expect", [
    ("Read", {"file_path": "/home/u/proj/src/app.py"}, None, "Читает src/app.py"),
    ("read", {"filePath": "/home/u/proj/src/app.py"}, None, "Читает src/app.py"),
    ("Read", {}, None, "Читает файл"),
    ("Edit", {"file_path": "/a/b/c/d.ts"}, None, "Редактирует c/d.ts"),
    ("MultiEdit", {"file_path": "x.py"}, None, "Редактирует x.py"),
    ("write", {"filePath": "/a/b/README.md"}, None, "Создаёт файл b/README.md"),
    ("Write", {"file_path": "/a/b/README.md"}, None, "Создаёт файл b/README.md"),
    ("patch", {}, None, "Применяет патч"),
    ("Glob", {"pattern": "**/*.py"}, None, "Ищет файлы по шаблону **/*.py"),
    ("glob", {"pattern": "*.ts"}, None, "Ищет файлы по шаблону *.ts"),
    ("Grep", {"pattern": "TODO", "path": "/x"}, None, "Ищет в коде: «TODO»"),
    ("grep", {"pattern": "foo"}, None, "Ищет в коде: «foo»"),
    ("list", {"path": "/a/b/c"}, None, "Просматривает папку b/c"),
    ("Bash", {"command": "npm test", "description": "Run tests"}, None, "Выполняет команду: Run tests"),
    ("bash", {"command": "ls   -la\n/tmp"}, None, "Выполняет команду: ls -la /tmp"),
    ("WebFetch", {"url": "https://example.com/a/b?q=1"}, None, "Загружает страницу example.com"),
    ("webfetch", {"url": "https://docs.python.org/3/"}, None, "Загружает страницу docs.python.org"),
    ("WebSearch", {"query": "asyncio timeout"}, None, "Ищет в интернете: asyncio timeout"),
    ("Task", {"description": "Исследовать код"}, None, "Запускает помощника: Исследовать код"),
    ("Agent", {"subagent_type": "explorer"}, None, "Запускает помощника: explorer"),
    ("task", {"description": "x"}, None, "Запускает помощника: x"),
    ("TodoWrite", {"todos": []}, None, "Обновляет список дел"),
    ("todowrite", {}, None, "Обновляет список дел"),
    ("NotebookEdit", {"notebook_path": "/n/a.ipynb"}, None, "Редактирует ноутбук n/a.ipynb"),
    ("mcp__orkestr__delegate", {"agent": "Программист", "task": "x"}, {"server": "orkestr", "tool": "delegate"},
     "Поручает задачу: Программист"),
    ("orkestr_delegate", {"agent": "Тестер"}, {"server": "orkestr", "tool": "delegate"}, "Поручает задачу: Тестер"),
    ("mcp__orkestr__delegate", {"agent": "Икс"}, None, "Поручает задачу: Икс"),
    ("mcp__orkestr__list_subordinates", {}, {"server": "orkestr", "tool": "list_subordinates"},
     "Смотрит список подчинённых"),
    ("mcp__orkestr__check_task", {"task_id": "1"}, {"server": "orkestr", "tool": "check_task"},
     "Проверяет поручённую задачу"),
    ("mcp__github__create_issue", {}, {"server": "github", "tool": "create_issue"}, "Вызывает инструмент github/create_issue"),
    ("mcp__fs__read", {}, None, "Вызывает инструмент fs/read"),
    ("Weird", {}, None, "Использует инструмент Weird"),
])
def test_describe_tool(name, inp, mcp, expect):
    assert describe_tool(name, inp, mcp) == expect


def test_describe_tool_never_raises_and_clips():
    assert describe_tool("Bash", None, None) == "Выполняет команду"        # type: ignore[arg-type]
    assert describe_tool("Read", {"file_path": 5}, None) == "Читает файл"
    assert describe_tool("", {}, None)
    long = describe_tool("Bash", {"command": "x" * 500}, None)
    assert len(long) < 100 and long.endswith("…")


# ------------------------------------------------------------------ DemoAdapter standalone
def spec(task="Сделай что-то", mcp_url=None, model="demo-cloud", tmp="/tmp"):
    return RunSpec(run_id="r1", agent={"name": "Бэкенд-разработчик", "role": "Пишет серверный код"}, task=task, cwd=tmp,
                   model=model, system_prompt="", permissions="edit", mcp_url=mcp_url)


async def collect(adapter, sp):
    return [ev async for ev in adapter.run(sp)]


async def test_demo_adapter_stream_shape():
    ad = DemoAdapter(speed=500, fail_rate=0.0)
    for _ in range(5):
        evs = await collect(ad, spec())
        types = [e.type for e in evs]
        assert types[0] == "session" and types[-1] == "result" and types.count("result") == 1
        assert {"thinking", "text", "tool_start", "tool_end", "usage"} <= set(types)
        starts = [e.data["tool_id"] for e in evs if e.type == "tool_start"]
        ends = [e.data["tool_id"] for e in evs if e.type == "tool_end"]
        assert starts == ends and len(starts) >= 2
        ctx = [e.data["context_used"] for e in evs if e.type == "usage"]
        assert ctx == sorted(ctx) and ctx[-1] > ctx[0] and max(ctx) <= 200_000
        res = evs[-1].data
        assert res["is_error"] is False and res["cost_usd"] > 0 and res["num_turns"] >= 2 and "Выполнил" in res["text"] or res["text"]
        assert all(e.data.get("mcp") is None for e in evs if e.type == "tool_start")
    local = await collect(ad, spec(model="demo-local"))
    assert local[-1].data["cost_usd"] == 0
    assert local[0].data["context_window"] == 32768


async def test_demo_adapter_failures():
    ad = DemoAdapter(speed=500, fail_rate=1.0)
    for _ in range(4):
        evs = await collect(ad, spec())
        assert evs[-1].type == "error" and evs[-1].data["fatal"] and "Демо" in evs[-1].data["message"]
        assert not any(e.type == "result" for e in evs)
    ad = DemoAdapter(speed=500)
    n = 0
    for _ in range(300):
        n += (await collect(ad, spec()))[-1].type == "error"
    assert 3 <= n <= 45, n                                               # ~7%


async def test_demo_adapter_detect_and_registry():
    info = await DemoAdapter().detect()
    assert info.available and {m.location for m in info.models} == {"local", "cloud"} and len(info.models) >= 2
    assert all(m.context_window for m in info.models)
    ads = build_adapters()
    assert "demo" in ads and ads["demo"].id == "demo"
    assert set(ads) <= {"claude_code", "opencode", "demo"}


async def test_demo_adapter_closes_cleanly_when_cancelled():
    ad = DemoAdapter(speed=1, fail_rate=0.0)
    gen = ad.run(spec())
    await gen.__anext__()
    await gen.aclose()                       # must not hang or leak tasks
    await asyncio.sleep(0)
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


# ------------------------------------------------------------------ seed / clear / driver
@pytest.fixture(params=["fake", "sqlite"])
def store(request, tmp_path):
    return make_store(request.param, tmp_path, EventBus())


async def test_seed_demo_team(store):
    out = seed_demo(store)
    agents = {a["name"]: a for a in out["agents"]}
    assert len(agents) in (5, 6) and all(a["demo"] and a["runtime"] == "demo" for a in agents.values())
    lead = agents[LEAD_NAME]
    subs = store.subordinates_of(lead["id"])
    assert 3 <= len(subs) <= 4
    # one of the specialists has its own subordinate
    assert any(store.subordinates_of(s["id"]) for s in subs)
    assert not store.supervisors_of(lead["id"])
    ys = {a["name"]: a["y"] for a in agents.values()}
    assert ys[LEAD_NAME] < min(y for n, y in ys.items() if n != LEAD_NAME)       # lead on top
    xs = sorted(s["x"] for s in subs)
    assert all(abs((b - a) - 260) <= 40 for a, b in zip(xs, xs[1:]))
    assert min(a["y"] for a in subs) - lead["y"] in range(160, 241)
    assert len({(a["x"], a["y"]) for a in agents.values()}) == len(agents)
    again = seed_demo(store)                                              # idempotent
    assert len(store.list_agents()) == len(agents) and len(store.list_edges()) == len(out["edges"]) == len(again["edges"])
    assert all(a.get("role") for a in agents.values())


async def test_clear_demo_cancels_runs_and_removes_everything(tmp_path):
    bus = EventBus()
    st = make_store("sqlite", tmp_path, bus)
    eng = RunEngine(st, bus, {"demo": DemoAdapter(speed=0.2, fail_rate=0)}, base_url="http://127.0.0.1:1")
    await eng.start()
    seed_demo(st)
    lead = st.get_agent_by_name(LEAD_NAME)
    plain = st.create_agent({"name": "Мой", "runtime": "demo", "demo": False})
    run = await eng.start_run(lead["id"], "задача")
    await wait_until(lambda: st.get_run(run["id"])["status"] == "running")
    sub = bus.subscribe()
    await clear_demo(st, eng)
    assert st.get_run(run["id"]) is None or st.get_run(run["id"])["status"] == "cancelled"
    assert [a["name"] for a in st.list_agents()] == ["Мой"] and st.list_edges() == []
    assert not st.list_runs(status="running,queued")
    kinds = []
    while True:
        try:
            kinds.append((await asyncio.wait_for(sub.__anext__(), 0.2))[0])
        except asyncio.TimeoutError:
            break
    assert "demo.cleared" in kinds
    sub.close()
    await eng.stop()
    del plain


async def test_driver_respects_flag_and_limit(tmp_path):
    release = asyncio.Event()

    async def script(spec):
        from orkestr.runtimes.base import RuntimeEvent
        await release.wait()
        yield RuntimeEvent("result", {"text": "ok", "is_error": False, "cost_usd": None, "num_turns": 1})

    env = await make_env("fake", tmp_path, script)
    seed_demo(env.store)
    drv = DemoDriver(env.engine, env.store)
    assert await drv.tick() is None                                   # demo_auto is off
    env.store.update_settings({"demo_auto": True})
    r1 = await drv.tick()
    r2 = await drv.tick()
    assert r1 and r2 and r1["task"] in DEMO_TASKS and r1["task"] != r2["task"] and r1["demo"]
    assert await drv.tick() is None                                   # never more than two demo roots
    # a user's own (non-demo) run does not count
    release.set()
    await env.engine.wait_run(r1["id"], 5)
    await env.engine.wait_run(r2["id"], 5)
    assert await drv.tick() is not None
    await env.engine.stop()


async def test_driver_loop_start_stop(tmp_path):
    env = await make_env("fake", tmp_path)
    seed_demo(env.store)
    env.store.update_settings({"demo_auto": True})
    drv = DemoDriver(env.engine, env.store, interval_s=(0.05, 0.1))
    await drv.start()
    await drv.start()                                                 # idempotent
    await wait_until(lambda: len(env.store.list_runs()) >= 2, timeout=3)
    await drv.stop()
    n = len(env.store.list_runs())
    await asyncio.sleep(0.3)
    assert len(env.store.list_runs()) == n
    await drv.stop()
    await env.engine.stop()
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()] == []


def test_driver_interval_default():
    assert DemoDriver(None, None).interval_s == (20.0, 40.0)
    assert len(DEMO_TASKS) >= 15
