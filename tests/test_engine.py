"""Engine tests: run pipeline, errors, limits, cancellation, external sessions, recovery.

Run against the in-memory fake store and the real SqliteStore.
"""
from __future__ import annotations

import asyncio

import pytest

from orkestr.interfaces import EngineError
from orkestr.runtimes.base import RuntimeEvent
from tests.fakes import FakeStore, ScriptedAdapter, make_env, wait_until



@pytest.fixture(params=["fake", "sqlite"])
def kind(request):
    return request.param


def E(type_, **data):
    return RuntimeEvent(type_, data)


async def finish(env):
    await env.engine.stop()
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not leftover, leftover


async def full_script(spec):
    yield E("session", session_id="sess-1", model="m-1", context_window=8000)
    yield E("thinking", text="думаю ", delta=True)
    yield E("thinking", text="вслух", delta=True)
    yield E("text", text="Смотрю ", delta=True)
    yield E("text", text="файл", delta=True)
    yield E("tool_start", tool_id="t1", name="Read", input={"file_path": "/a/b/src/app.py"}, mcp=None)
    await asyncio.sleep(0.02)
    yield E("tool_end", tool_id="t1", output="print(1)", is_error=False)
    yield E("usage", input_tokens=100, output_tokens=20, cache_read_tokens=5, cache_write_tokens=1,
            context_used=1200, context_window=8000, cost_usd=0.01)
    yield E("tool_start", tool_id="t2", name="Bash", input={"command": "pytest", "x": "y" * 5000}, mcp=None)
    yield E("tool_end", tool_id="t2", output="boom", is_error=True)
    yield E("text", text="Готово.", delta=False)
    yield E("usage", input_tokens=50, output_tokens=10, cache_read_tokens=0, cache_write_tokens=0,
            context_used=1500, context_window=8000, cost_usd=0.005)
    yield E("result", text="Итог работы", is_error=False, cost_usd=0.02, num_turns=3)


async def test_full_run(kind, tmp_path):
    env = await make_env(kind, tmp_path, full_script)
    a = env.agent("Один", model="m-1")
    run = await env.engine.start_run(a["id"], "  сделай  ")
    assert run["status"] == "running" and run["task"] == "сделай"
    assert run["origin"] == "user" and run["depth"] == 0 and run["root_run_id"] == run["id"]
    done = await env.engine.wait_run(run["id"], 5)
    assert done["status"] == "ok" and done["result"] == "Итог работы"
    assert done["session_id"] == "sess-1" and done["model"] == "m-1"
    assert (done["tokens_in"], done["tokens_out"]) == (150, 30)
    assert done["cache_read_tokens"] == 5 and done["cache_write_tokens"] == 1
    assert done["cost_usd"] == pytest.approx(0.02)          # result total overrides deltas
    assert (done["context_used"], done["context_window"]) == (1500, 8000)
    assert done["num_turns"] == 3 and done["tool_calls"] == 2 and done["delegations"] == 0
    assert done["ended_at"] and done["duration_ms"] is not None and done["activity"] is None
    steps = env.steps(run["id"])
    assert [s["kind"] for s in steps] == ["thinking", "message", "tool", "tool", "message"]
    assert steps[0]["output"] == "думаю вслух" and steps[0]["status"] == "ok"
    assert steps[1]["output"] == "Смотрю файл" and steps[1]["name"] == "assistant"
    assert steps[2]["title"] == "Читает src/app.py" and steps[2]["output"] == "print(1)"
    assert steps[2]["status"] == "ok" and steps[2]["duration_ms"] >= 0 and steps[2]["tool_id"] == "t1"
    assert steps[3]["status"] == "error" and steps[3]["output"] == "boom"
    assert len(steps[3]["input"]["x"]) <= 4000                # input truncated
    assert steps[4]["output"] == "Готово."
    assert [s["seq"] for s in steps] == sorted(s["seq"] for s in steps)
    assert all(s["status"] != "running" for s in steps)
    await finish(env)


async def test_activity_visible_while_running(kind, tmp_path):
    gate = asyncio.Event()

    async def script(spec):
        yield E("tool_start", tool_id="t", name="Grep", input={"pattern": "TODO"}, mcp=None)
        await gate.wait()
        yield E("result", text="ok", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script)
    a = env.agent("A")
    run = await env.engine.start_run(a["id"], "t")
    await wait_until(lambda: (env.store.get_run(run["id"]) or {}).get("activity", "").startswith("Ищет в коде"))
    assert env.store.get_agent(a["id"])["live"]["status"] == "working"
    gate.set()
    assert (await env.engine.wait_run(run["id"], 5))["status"] == "ok"
    await finish(env)


async def test_streaming_throttle_and_final_text(kind, tmp_path):
    pieces = [f"w{i} " for i in range(120)]

    async def script(spec):
        for p in pieces:
            yield E("text", text=p, delta=True)
            await asyncio.sleep(0.005)
        yield E("result", text="".join(pieces), is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script)
    a = env.agent("A")
    t0 = asyncio.get_event_loop().time()
    run = await env.engine.start_run(a["id"], "t")
    await env.engine.wait_run(run["id"], 10)
    elapsed = asyncio.get_event_loop().time() - t0
    (step,) = env.steps(run["id"])
    assert step["output"] == "".join(pieces) and step["status"] == "ok"
    if kind == "fake":
        updates = env.store.step_updates[step["id"]]
        # ~5/s plus the final write; never one update per delta
        assert updates <= int(elapsed * 5) + 3, (updates, elapsed)
        assert updates >= 2
    await finish(env)


async def test_trailing_flush_when_stream_pauses(tmp_path):
    gate = asyncio.Event()

    async def script(spec):
        yield E("text", text="a", delta=True)
        yield E("text", text="b", delta=True)   # inside the throttle window
        await gate.wait()
        yield E("result", text="ab", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env("fake", tmp_path, script)
    a = env.agent("A")
    run = await env.engine.start_run(a["id"], "t")
    await wait_until(lambda: any(s["output"] == "ab" for s in env.steps(run["id"])), timeout=2)
    gate.set()
    await env.engine.wait_run(run["id"], 5)
    await finish(env)


async def test_complete_block_after_deltas_replaces_not_duplicates(tmp_path):
    async def script(spec):
        yield E("text", text="Привет, ", delta=True)
        yield E("text", text="мир", delta=True)
        yield E("text", text="Привет, мир", delta=False)
        yield E("result", text="Привет, мир", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env("fake", tmp_path, script)
    a = env.agent("A")
    run = await env.engine.start_run(a["id"], "t")
    await env.engine.wait_run(run["id"], 5)
    steps = env.steps(run["id"])
    assert len(steps) == 1 and steps[0]["output"] == "Привет, мир"
    await finish(env)


async def test_long_output_truncated(kind, tmp_path):
    async def script(spec):
        yield E("tool_start", tool_id="t", name="Read", input={"file_path": "x"}, mcp=None)
        yield E("tool_end", tool_id="t", output="z" * 30000, is_error=False)
        yield E("result", text="r", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script)
    a = env.agent("A")
    run = await env.engine.start_run(a["id"], "t")
    await env.engine.wait_run(run["id"], 5)
    (step,) = env.steps(run["id"])
    assert len(step["output"]) <= 20000
    await finish(env)


# ---------------------------------------------------------------- failure paths
async def test_error_paths(kind, tmp_path):
    async def is_error(spec):
        yield E("text", text="частичный ответ", delta=False)
        yield E("result", text="Не получилось", is_error=True, cost_usd=None, num_turns=1)

    async def fatal(spec):
        yield E("text", text="начал", delta=True)
        yield E("tool_start", tool_id="t", name="Bash", input={"command": "ls"}, mcp=None)
        yield E("error", message="CLI упал", fatal=True)
        yield E("text", text="не должно быть обработано", delta=False)

    async def nonfatal(spec):
        yield E("error", message="предупреждение", fatal=False)
        yield E("result", text="всё равно ок", is_error=False, cost_usd=None, num_turns=1)

    async def no_result(spec):
        yield E("text", text="ничего больше", delta=False)

    async def crash(spec):
        yield E("text", text="x", delta=False)
        raise RuntimeError("bug in adapter")

    cases = [(is_error, "error", "Не получилось"), (fatal, "error", "CLI упал"),
             (nonfatal, "ok", None), (no_result, "error", "без итогового"), (crash, "error", "bug in adapter")]
    for script, status, fragment in cases:
        sub = tmp_path / script.__name__
        sub.mkdir()
        env = await make_env(kind, sub, script)
        a = env.agent("A")
        run = await env.engine.start_run(a["id"], "t")
        done = await env.engine.wait_run(run["id"], 5)
        assert done["status"] == status, (script.__name__, done)
        if fragment:
            assert fragment in (done["error"] or "")
        steps = env.steps(run["id"])
        assert all(s["status"] != "running" for s in steps)
        if script is fatal:
            assert not any(s["output"] == "не должно быть обработано" for s in steps)
            assert [s for s in steps if s["kind"] == "tool"][0]["status"] == "error"
            assert env.adapter.closed == 1
        if script is nonfatal:
            assert steps[0]["kind"] == "error" and done["result"] == "всё равно ок"
        await finish(env)


async def test_run_timeout(kind, tmp_path):
    async def script(spec):
        yield E("text", text="x", delta=False)
        await asyncio.sleep(60)
        yield E("result", text="never", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script)
    real = env.store.get_settings
    env.store.get_settings = lambda: {**real(), "run_timeout_s": 1}   # the real store accepts >= 10 only
    a = env.agent("A")
    run = await env.engine.start_run(a["id"], "t")
    done = await env.engine.wait_run(run["id"], 6)
    assert done["status"] == "error" and "Превышено время" in done["error"]
    assert env.adapter.closed == 1 and env.adapter.live == 0
    await finish(env)


# ---------------------------------------------------------------- validation
async def test_validation(kind, tmp_path):
    env = await make_env(kind, tmp_path)
    a = env.agent("A")
    with pytest.raises(EngineError) as e:
        await env.engine.start_run(a["id"], "   ")
    assert e.value.code == "empty_task"
    with pytest.raises(EngineError) as e:
        await env.engine.start_run("nope", "t")
    assert e.value.code == "unknown_agent"
    with pytest.raises(EngineError) as e:
        await env.engine.start_run(a["id"], "t", cwd=str(tmp_path / "missing"))
    assert e.value.code == "bad_cwd"
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(EngineError) as e:
        await env.engine.start_run(a["id"], "t", cwd=str(f))
    assert e.value.code == "bad_cwd"
    other = env.agent("Other", runtime="opencode") if kind == "sqlite" else None
    if other:
        with pytest.raises(EngineError) as e:
            await env.engine.start_run(other["id"], "t")
        assert e.value.code == "runtime_unavailable"
    await finish(env)


async def test_runtime_unavailable(kind, tmp_path):
    ad = ScriptedAdapter(adapter_id="demo", available=False)
    env = await make_env(kind, tmp_path, adapter=ad)
    a = env.agent("A")
    with pytest.raises(EngineError) as e:
        await env.engine.start_run(a["id"], "t")
    assert e.value.code == "runtime_unavailable" and "нет CLI" in str(e.value)
    ad.available = True                                   # becomes available later
    env.engine._info_at["demo"] -= 100                    # cached verdict is old enough to re-check
    run = await env.engine.start_run(a["id"], "t")
    assert (await env.engine.wait_run(run["id"], 5))["status"] == "ok"
    await finish(env)


async def test_cwd_resolution(kind, tmp_path):
    sub = tmp_path / "proj"
    sub.mkdir()
    env = await make_env(kind, tmp_path)
    a = env.agent("A", cwd=str(sub))
    b = env.agent("B")
    r1 = await env.engine.start_run(a["id"], "t")
    r2 = await env.engine.start_run(b["id"], "t")
    r3 = await env.engine.start_run(a["id"], "t", cwd=str(tmp_path))
    assert (r1["cwd"], r2["cwd"], r3["cwd"]) == (str(sub), str(tmp_path), str(tmp_path))
    for r in (r1, r2, r3):
        await env.engine.wait_run(r["id"], 5)
    assert [s.cwd for s in env.adapter.specs].count(str(sub)) == 1
    await finish(env)


async def test_depth_limit(kind, tmp_path):
    gate = asyncio.Event()

    async def script(spec):
        await gate.wait()
        yield E("result", text="r", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script, max_depth=1)
    a = env.agent("A")
    root = await env.engine.start_run(a["id"], "t")
    child = await env.engine.start_run(a["id"], "t", origin="delegation", parent_run_id=root["id"])
    assert child["depth"] == 1 and child["root_run_id"] == root["id"] and child["parent_run_id"] == root["id"]
    with pytest.raises(EngineError) as e:
        await env.engine.start_run(a["id"], "t", origin="delegation", parent_run_id=child["id"])
    assert e.value.code == "depth_exceeded"
    gate.set()
    await env.engine.wait_run(root["id"], 5)
    await finish(env)


async def test_prompt_and_mcp_url(kind, tmp_path):
    async def script(spec):
        yield E("result", text="r", is_error=False, cost_usd=None, num_turns=1)

    class P:
        def local_servers_up(self):
            return [{"id": "lm", "kind": "lmstudio"}]

    env = await make_env(kind, tmp_path, script, probes=P())
    boss = env.agent("Босс", role="руководитель", system_prompt="Будь краток.")
    dev = env.agent("Дев", role="пишет код")
    env.link(boss, dev)
    r1 = await env.engine.start_run(boss["id"], "t")
    r2 = await env.engine.start_run(dev["id"], "t")
    await env.engine.wait_run(r1["id"], 5)
    await env.engine.wait_run(r2["id"], 5)
    s_boss = next(s for s in env.adapter.specs if s.run_id == r1["id"])
    s_dev = next(s for s in env.adapter.specs if s.run_id == r2["id"])
    assert s_boss.mcp_url.startswith("http://127.0.0.1:9999/mcp/run/") and len(s_boss.mcp_url.rsplit("/", 1)[1]) >= 24
    assert s_dev.mcp_url is None
    assert "Будь краток." in s_boss.system_prompt and "руководитель" in s_boss.system_prompt
    assert "list_subordinates" in s_boss.system_prompt and "delegate" in s_boss.system_prompt
    assert "list_subordinates" not in s_dev.system_prompt
    assert "языке задачи" in s_dev.system_prompt and "пишет код" in s_dev.system_prompt
    assert s_boss.local_servers == [{"id": "lm", "kind": "lmstudio"}]
    assert s_boss.agent["name"] == "Босс" and s_boss.permissions == "edit"
    # token dies with the run
    token = s_boss.mcp_url.rsplit("/", 1)[1]
    assert env.engine.caller_for_token(token) is None
    await finish(env)


# ---------------------------------------------------------------- queue / slots
async def test_queue_limit(kind, tmp_path):
    release = asyncio.Event()

    async def script(spec):
        await release.wait()
        yield E("result", text="r", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script, max_concurrent_runs=2)
    a = env.agent("A")
    runs = [await env.engine.start_run(a["id"], f"t{i}") for i in range(5)]
    assert [r["status"] for r in runs] == ["running", "running", "queued", "queued", "queued"]
    await wait_until(lambda: env.adapter.live == 2)
    await asyncio.sleep(0.05)
    assert env.adapter.live == 2 and env.store.get_run(runs[2]["id"])["status"] == "queued"
    release.set()
    for r in runs:
        assert (await env.engine.wait_run(r["id"], 5))["status"] == "ok"
    assert env.adapter.max_live == 2
    await finish(env)


async def test_cancel_queued_run(kind, tmp_path):
    release = asyncio.Event()

    async def script(spec):
        await release.wait()
        yield E("result", text="r", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script, max_concurrent_runs=1)
    a = env.agent("A")
    r1 = await env.engine.start_run(a["id"], "1")
    r2 = await env.engine.start_run(a["id"], "2")
    r3 = await env.engine.start_run(a["id"], "3")
    assert r2["status"] == "queued"
    assert await env.engine.cancel_run(r2["id"]) is True
    assert env.store.get_run(r2["id"])["status"] == "cancelled"
    release.set()
    assert (await env.engine.wait_run(r3["id"], 5))["status"] == "ok"
    assert env.adapter.started == 2                        # the cancelled one never started
    assert (await env.engine.wait_run(r1["id"], 5))["status"] == "ok"
    assert await env.engine.cancel_run(r1["id"]) is False  # already finished
    assert await env.engine.cancel_run("nope") is False
    await finish(env)


async def test_cancel_immediately_after_start(kind, tmp_path):
    async def script(spec):
        await asyncio.sleep(30)
        yield E("result", text="r", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script)
    a = env.agent("A")
    run = await env.engine.start_run(a["id"], "t")
    assert await env.engine.cancel_run(run["id"]) is True      # before the task even started
    done = env.store.get_run(run["id"])
    assert done["status"] == "cancelled" and done["ended_at"]
    await finish(env)


async def test_cancel_running_closes_generator(kind, tmp_path):
    state = {"cleaned": False}

    async def script(spec):
        try:
            yield E("tool_start", tool_id="t", name="Bash", input={"command": "sleep 99"}, mcp=None)
            await asyncio.sleep(60)
            yield E("result", text="r", is_error=False, cost_usd=None, num_turns=1)
        finally:
            state["cleaned"] = True                        # stands for "kill the subprocess group"

    env = await make_env(kind, tmp_path, script)
    a = env.agent("A")
    run = await env.engine.start_run(a["id"], "t")
    await wait_until(lambda: env.steps(run["id"]))
    assert await env.engine.cancel_run(run["id"])
    done = env.store.get_run(run["id"])
    assert done["status"] == "cancelled" and done["result"] is None
    assert state["cleaned"] and env.adapter.live == 0
    assert env.steps(run["id"])[0]["status"] == "error"
    assert env.store.get_agent(a["id"])["live"]["status"] != "working"
    await finish(env)


async def test_stop_cancels_everything_and_leaves_no_tasks(kind, tmp_path):
    async def script(spec):
        yield E("text", text="x", delta=False)
        await asyncio.sleep(60)
        yield E("result", text="r", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script, max_concurrent_runs=2)
    a = env.agent("A")
    runs = [await env.engine.start_run(a["id"], f"{i}") for i in range(4)]
    await wait_until(lambda: env.adapter.live == 2)
    await env.engine.stop()
    assert all(env.store.get_run(r["id"])["status"] == "cancelled" for r in runs)
    assert env.adapter.live == 0 and env.adapter.closed == 2
    with pytest.raises(EngineError):
        await env.engine.start_run(a["id"], "after stop")
    await finish(env)


# ---------------------------------------------------------------- delegation (engine API)
def delegating_parent(calls, *, start_first=True, same_agent_twice=False):
    """Parent script that delegates through engine.mcp_delegate like the MCP layer would."""
    holder: dict = {}

    async def script(spec):
        if spec.agent["name"] != "Босс":
            yield E("text", text=f"сделал: {spec.task}", delta=False)
            yield E("result", text=f"итог({spec.task})", is_error=False, cost_usd=None, num_turns=1)
            return
        eng = holder["engine"]
        jobs = [("Дев", "задача-1")] + ([("Дев", "задача-2")] if same_agent_twice else [])
        results: dict[str, dict] = {}

        async def one(i, agent, task):
            tid = f"d{i}"
            mcp = {"server": "orkestr", "tool": "delegate"}
            ev = E("tool_start", tool_id=tid, name="mcp__orkestr__delegate", input={"agent": agent, "task": task}, mcp=mcp)
            if start_first:
                calls.put_nowait(ev)
                await asyncio.sleep(0.02)
                r = await eng.mcp_delegate(("run", spec.run_id), agent, task)
            else:
                t = asyncio.ensure_future(eng.mcp_delegate(("run", spec.run_id), agent, task))
                await asyncio.sleep(0.05)
                calls.put_nowait(ev)
                r = await t
            results[tid] = r
            calls.put_nowait(E("tool_end", tool_id=tid, output=r.get("result") or "", is_error=r["status"] != "ok"))

        tasks = [asyncio.create_task(one(i, a, t)) for i, (a, t) in enumerate(jobs)]
        pending = len(tasks)
        while pending:
            ev = await calls.get()
            yield ev
            if ev.type == "tool_end":
                pending -= 1
        await asyncio.gather(*tasks)
        yield E("result", text="; ".join(r["result"] for r in results.values()), is_error=False,
                cost_usd=None, num_turns=2)

    return script, holder


@pytest.mark.parametrize("start_first", [True, False])
@pytest.mark.parametrize("twice", [False, True])
async def test_delegation_linking_both_orders(kind, tmp_path, start_first, twice):
    calls: asyncio.Queue = asyncio.Queue()
    script, holder = delegating_parent(calls, start_first=start_first, same_agent_twice=twice)
    env = await make_env(kind, tmp_path, script)
    holder["engine"] = env.engine
    boss, dev = env.agent("Босс"), env.agent("Дев", role="пишет код")
    env.link(boss, dev)
    root = await env.engine.start_run(boss["id"], "большая задача")
    done = await env.engine.wait_run(root["id"], 10)
    assert done["status"] == "ok", done
    n = 2 if twice else 1
    assert done["delegations"] == n
    steps = [s for s in env.steps(root["id"]) if s["kind"] == "delegation"]
    assert len(steps) == n
    tree = env.store.run_tree(root["id"])
    children = [r for r in tree if r["id"] != root["id"]]
    assert len(children) == n
    for s in steps:
        assert s["child_run_id"], s
        child = env.store.get_run(s["child_run_id"])
        assert child["parent_step_id"] == s["id"] and child["parent_run_id"] == root["id"]
        assert child["origin"] == "delegation" and child["depth"] == 1 and child["root_run_id"] == root["id"]
        # the step's input task is the child's task
        assert child["task"] == s["input"]["task"]
        assert s["status"] == "ok" and s["title"] == "Поручает задачу: Дев"
        assert s["output"] == f"итог({child['task']})"
    assert len({s["child_run_id"] for s in steps}) == n
    await finish(env)


async def test_delegation_refusals_and_live_hierarchy(kind, tmp_path):
    gate = asyncio.Event()

    async def script(spec):
        await gate.wait()
        yield E("result", text="r", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script)
    from orkestr.engine import DelegationError

    boss, dev, ops, stranger = env.agent("Босс"), env.agent("Дев"), env.agent("Опс"), env.agent("Чужой")
    env.link(boss, dev)
    root = await env.engine.start_run(boss["id"], "t")
    caller = ("run", root["id"])
    subs = await env.engine.mcp_list_subordinates(caller)
    assert [s["name"] for s in subs] == ["Дев"] and set(subs[0]) == {"name", "role", "runtime", "model", "status"}
    with pytest.raises(DelegationError) as e:
        await env.engine.mcp_delegate(caller, "Чужой", "x")
    assert "не ваш подчинённый" in str(e.value) and "«Дев»" in str(e.value)
    with pytest.raises(DelegationError):
        await env.engine.mcp_delegate(caller, "Дев", "  ")
    env.link(boss, ops)                                    # drawn while the run is active
    assert {s["name"] for s in await env.engine.mcp_list_subordinates(caller)} == {"Дев", "Опс"}
    with pytest.raises(DelegationError):
        await env.engine.mcp_check_task(caller, "unknown")
    gate.set()
    await env.engine.wait_run(root["id"], 5)
    with pytest.raises(DelegationError):                   # run is over
        await env.engine.mcp_list_subordinates(caller)
    await finish(env)


async def test_depth_refusal_message(kind, tmp_path):
    gate = asyncio.Event()

    async def script(spec):
        await gate.wait()
        yield E("result", text="r", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script, max_depth=0)
    from orkestr.engine import DelegationError

    boss, dev = env.agent("Босс"), env.agent("Дев")
    env.link(boss, dev)
    root = await env.engine.start_run(boss["id"], "t")
    with pytest.raises(DelegationError) as e:
        await env.engine.mcp_delegate(("run", root["id"]), "Дев", "x")
    assert "глубина" in str(e.value)
    gate.set()
    await finish(env)


async def test_cancel_kills_descendants(kind, tmp_path):
    cleaned: list[str] = []
    holder: dict = {}

    async def script(spec):
        name = spec.agent["name"]
        try:
            if name == "Босс":
                yield E("text", text="поручаю", delta=False)
                await holder["engine"].mcp_delegate(("run", spec.run_id), "Дев", "работа")
            elif name == "Дев":
                await holder["engine"].mcp_delegate(("run", spec.run_id), "Стажёр", "мелочь")
            else:
                await asyncio.sleep(60)
            yield E("result", text="r", is_error=False, cost_usd=None, num_turns=1)
        finally:
            cleaned.append(name)

    env = await make_env(kind, tmp_path, script)
    holder["engine"] = env.engine
    boss, dev, tr = env.agent("Босс"), env.agent("Дев"), env.agent("Стажёр")
    env.link(boss, dev)
    env.link(dev, tr)
    root = await env.engine.start_run(boss["id"], "t")
    await wait_until(lambda: len(env.store.run_tree(root["id"])) == 3 and env.adapter.live == 3)
    assert await env.engine.cancel_run(root["id"])
    tree = env.store.run_tree(root["id"])
    assert {r["status"] for r in tree} == {"cancelled"} and len(tree) == 3
    assert sorted(cleaned) == ["Босс", "Дев", "Стажёр"] and env.adapter.live == 0
    assert not env.engine._slots and not env.engine._queue and not env.engine._tokens
    await finish(env)


async def test_cancel_child_alone_reports_to_parent(kind, tmp_path):
    holder: dict = {}
    seen: dict = {}

    async def script(spec):
        if spec.agent["name"] == "Босс":
            r = await holder["engine"].mcp_delegate(("run", spec.run_id), "Дев", "работа")
            seen["r"] = r
            yield E("result", text=f"child={r['status']}", is_error=False, cost_usd=None, num_turns=1)
        else:
            await asyncio.sleep(60)
            yield E("result", text="x", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script)
    holder["engine"] = env.engine
    boss, dev = env.agent("Босс"), env.agent("Дев")
    env.link(boss, dev)
    root = await env.engine.start_run(boss["id"], "t")
    await wait_until(lambda: len(env.store.run_tree(root["id"])) == 2)
    child = next(r for r in env.store.run_tree(root["id"]) if r["id"] != root["id"])
    await env.engine.cancel_run(child["id"])
    done = await env.engine.wait_run(root["id"], 5)
    assert done["status"] == "ok" and done["result"] == "child=cancelled"
    await finish(env)


async def test_parent_ending_cancels_unfinished_children(kind, tmp_path):
    holder: dict = {}

    async def script(spec):
        if spec.agent["name"] == "Босс":
            t = asyncio.ensure_future(holder["engine"].mcp_delegate(("run", spec.run_id), "Дев", "долго"))
            await wait_until(lambda: holder["engine"]._ctx.get(spec.run_id) and holder["engine"]._ctx[spec.run_id].children)
            yield E("result", text="закончил, не дожидаясь", is_error=False, cost_usd=None, num_turns=1)
            t.cancel()
        else:
            await asyncio.sleep(60)
            yield E("result", text="x", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script)
    holder["engine"] = env.engine
    boss, dev = env.agent("Босс"), env.agent("Дев")
    env.link(boss, dev)
    root = await env.engine.start_run(boss["id"], "t")
    await env.engine.wait_run(root["id"], 5)
    await wait_until(lambda: env.adapter.live == 0)
    child = next(r for r in env.store.run_tree(root["id"]) if r["id"] != root["id"])
    assert env.store.get_run(child["id"])["status"] == "cancelled"
    await finish(env)


async def test_no_deadlock_when_children_need_the_parents_slot(kind, tmp_path):
    """max_concurrent_runs=1: the waiting parent releases its slot, so the child can run."""
    holder: dict = {}
    order: list[str] = []

    async def script(spec):
        n = spec.agent["name"]
        order.append(f"start:{n}")
        if n == "Босс":
            r = await holder["engine"].mcp_delegate(("run", spec.run_id), "Дев", "работа")
            order.append("boss-got-result")
            yield E("result", text=r["result"], is_error=False, cost_usd=None, num_turns=1)
        elif n == "Дев":
            r = await holder["engine"].mcp_delegate(("run", spec.run_id), "Стажёр", "мелочь")
            yield E("result", text=r["result"], is_error=False, cost_usd=None, num_turns=1)
        else:
            await asyncio.sleep(0.05)
            yield E("result", text="мелочь готова", is_error=False, cost_usd=None, num_turns=1)
        order.append(f"end:{n}")

    env = await make_env(kind, tmp_path, script, max_concurrent_runs=1)
    holder["engine"] = env.engine
    boss, dev, tr, other = env.agent("Босс"), env.agent("Дев"), env.agent("Стажёр"), env.agent("Другой")
    env.link(boss, dev)
    env.link(dev, tr)
    root = await env.engine.start_run(boss["id"], "t")
    stranger = await env.engine.start_run(other["id"], "посторонний")   # queued behind the tree
    assert stranger["status"] == "queued"
    done = await env.engine.wait_run(root["id"], 10)
    assert done["status"] == "ok" and done["result"] == "мелочь готова"
    assert (await env.engine.wait_run(stranger["id"], 10))["status"] == "ok"
    # the delegated chain ran before the unrelated queued run started
    assert order.index("start:Другой") > order.index("boss-got-result")
    assert order.index("start:Стажёр") < order.index("start:Другой")
    await finish(env)


async def test_blocked_parent_does_not_hold_slot_but_limit_holds(kind, tmp_path):
    """With limit 2: parent waits on a slow child; two more user runs: only one fits next to the child."""
    holder: dict = {}
    release = asyncio.Event()

    async def script(spec):
        n = spec.agent["name"]
        if n == "Босс":
            r = await holder["engine"].mcp_delegate(("run", spec.run_id), "Дев", "работа")
            yield E("result", text=r["result"], is_error=False, cost_usd=None, num_turns=1)
        else:
            await release.wait()
            yield E("result", text="ok", is_error=False, cost_usd=None, num_turns=1)

    env = await make_env(kind, tmp_path, script, max_concurrent_runs=2)
    holder["engine"] = env.engine
    boss, dev, o1, o2 = env.agent("Босс"), env.agent("Дев"), env.agent("О1"), env.agent("О2")
    env.link(boss, dev)
    root = await env.engine.start_run(boss["id"], "t")
    await wait_until(lambda: len(env.store.run_tree(root["id"])) == 2)
    r1 = await env.engine.start_run(o1["id"], "1")
    r2 = await env.engine.start_run(o2["id"], "2")
    # slots: child + r1 (the blocked parent holds none); r2 has to wait
    assert r1["status"] == "running" and r2["status"] == "queued"
    release.set()
    for r in (root, r1, r2):
        assert (await env.engine.wait_run(r["id"], 10))["status"] == "ok"
    await finish(env)


# ---------------------------------------------------------------- external sessions
async def test_external_sessions(kind, tmp_path):
    env = await make_env(kind, tmp_path)
    eng = env.engine
    rid = eng.external_begin("k1", {"runtime": "claude_code", "title": "Починить тесты", "cwd": str(tmp_path),
                                    "model": "claude-x", "session_id": "S1", "started_at": 1_700_000_000_000})
    assert eng.external_begin("k1", {"runtime": "claude_code", "title": "other"}) == rid     # idempotent
    run = env.store.get_run(rid)
    assert run["origin"] == "external" and run["agent_id"] is None and run["status"] == "running"
    assert run["started_at"] == 1_700_000_000_000 and run["session_id"] == "S1" and run["task"] == "Починить тесты"
    ts = 1_700_000_001_000
    eng.external_event(rid, E("text", text="Начинаю", delta=False), ts)
    eng.external_event(rid, E("tool_start", tool_id="x", name="Edit", input={"file_path": "/p/src/a.py"}, mcp=None), ts + 10)
    eng.external_event(rid, E("tool_end", tool_id="x", output="ok", is_error=False), ts + 60)
    eng.external_event(rid, E("usage", input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0,
                              context_used=100, context_window=1000, cost_usd=0.1), ts + 70)
    sub = eng.external_begin("k1/sub", {"runtime": "claude_code", "title": "Субагент", "parent_key": "k1"})
    srun = env.store.get_run(sub)
    assert srun["parent_run_id"] == rid and srun["root_run_id"] == rid and srun["depth"] == 1
    eng.external_event(sub, E("text", text="субагент работает", delta=False), ts + 80)
    eng.external_end(sub, "ok", ts + 90)
    eng.external_end(rid, "ok", ts + 100)
    run = env.store.get_run(rid)
    assert run["status"] == "ok" and run["ended_at"] == ts + 100 and run["duration_ms"] == ts + 100 - 1_700_000_000_000
    assert run["tool_calls"] == 1 and run["tokens_in"] == 10 and run["context_used"] == 100
    assert run["result"] == "Начинаю"
    steps = env.steps(rid)
    assert [s["kind"] for s in steps] == ["message", "tool"] and steps[1]["duration_ms"] == 50
    assert steps[1]["started_at"] == ts + 10
    assert env.store.get_run(sub)["status"] == "ok"
    # events after the end are ignored; the key can be reopened
    eng.external_event(rid, E("text", text="поздно", delta=False), ts + 200)
    assert len(env.steps(rid)) == 2
    assert eng.external_begin("k1", {"runtime": "claude_code"}) == rid
    assert env.store.get_run(rid)["status"] == "running"
    eng.external_event(rid, E("text", text="продолжение", delta=False))
    assert [s["seq"] for s in env.steps(rid)] == [1, 2, 3]
    eng.external_end(rid, "cancelled")
    assert env.store.get_run(rid)["status"] == "cancelled"
    eng.external_event("unknown", E("text", text="x", delta=False))      # no crash
    eng.external_end("unknown")
    assert await eng.cancel_run(rid) is False
    await finish(env)


async def test_external_stream_throttled_no_timers_left(tmp_path):
    env = await make_env("fake", tmp_path)
    rid = env.engine.external_begin("k", {"runtime": "claude_code", "title": "t"})
    for i in range(50):
        env.engine.external_event(rid, E("text", text=f"{i} ", delta=True))
    env.engine.external_end(rid, "ok")
    (step,) = env.steps(rid)
    assert step["output"].startswith("0 1 2") and step["output"].endswith("49 ") and step["status"] == "ok"
    assert env.store.step_updates[step["id"]] <= 4
    await asyncio.sleep(0.3)           # a leaked timer would fire and fail/overwrite here
    await finish(env)


# ---------------------------------------------------------------- crash recovery
async def test_crash_recovery(tmp_path):
    from orkestr.bus import EventBus
    from orkestr.engine import RunEngine

    bus = EventBus()
    store = FakeStore(bus, default_cwd=str(tmp_path))
    a = store.create_agent({"name": "A", "runtime": "demo"})
    base = {"agent_id": a["id"], "agent_name": "A", "runtime": "demo", "model": None, "task": "t",
            "origin": "user", "parent_run_id": None, "depth": 0, "started_at": 1000, "tokens_in": 0, "tokens_out": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0}
    r1 = store.create_run({**base, "id": "r-run", "status": "running"})
    r2 = store.create_run({**base, "id": "r-queued", "status": "queued"})
    r3 = store.create_run({**base, "id": "r-ok", "status": "ok", "ended_at": 1500})
    s = store.create_step({"run_id": r1["id"], "seq": 1, "kind": "tool", "name": "Bash", "title": "x",
                           "status": "running", "started_at": 1100})
    engine = RunEngine(store, bus, {"demo": ScriptedAdapter()}, base_url="http://x")
    await engine.start()
    assert store.get_run("r-run")["status"] == "error" and store.get_run("r-queued")["status"] == "error"
    assert "перезапущен" in store.get_run("r-run")["error"]
    assert store.get_run("r-run")["ended_at"] and store.get_run("r-ok")["status"] == "ok"
    assert store.get_step(s["id"])["status"] == "error"
    await engine.stop()


async def test_crash_recovery_real_store_file(tmp_path):
    """A run left running in the db file is an error after the next start."""
    from orkestr.bus import EventBus
    from orkestr.engine import RunEngine
    from orkestr.store import SqliteStore

    db = str(tmp_path / "x.db")
    st1 = SqliteStore(db, EventBus())
    a = st1.create_agent({"name": "A", "runtime": "demo"})
    run = st1.create_run({"agent_id": a["id"], "task": "t", "status": "running", "origin": "user"})
    st1.close()
    st2 = SqliteStore(db, EventBus())
    engine = RunEngine(st2, EventBus(), {"demo": ScriptedAdapter()}, base_url="http://x")
    await engine.start()
    got = st2.get_run(run["id"])
    assert got["status"] == "error" and "перезапущен" in got["error"]
    await engine.stop()
    st2.close()


# ---------------------------------------------------------------- runtimes
async def test_refresh_runtimes(tmp_path):
    from orkestr.bus import EventBus
    from orkestr.engine import RunEngine
    import orkestr.engine as eng_mod

    class Slow(ScriptedAdapter):
        async def detect(self, local_servers=None):
            await asyncio.sleep(60)

    class Boom(ScriptedAdapter):
        async def detect(self, local_servers=None):
            raise RuntimeError("detect exploded")

    seen = {}

    class Rec(ScriptedAdapter):
        async def detect(self, local_servers=None):
            seen["servers"] = local_servers
            return await super().detect(local_servers)

    class P:
        def local_servers_up(self):
            return [{"id": "lm"}]

    eng_mod.DETECT_TIMEOUT_S = 0.2
    bus = EventBus()
    store = FakeStore(bus, default_cwd=str(tmp_path))
    engine = RunEngine(store, bus, {"slow": Slow(adapter_id="slow"), "boom": Boom(adapter_id="boom"),
                                    "ok": Rec(adapter_id="ok")}, P(), base_url="http://x")
    sub = bus.subscribe()
    before = engine.runtimes_info()
    assert [r["id"] for r in before] == ["slow", "boom", "ok"] and not any(r["available"] for r in before)
    t0 = asyncio.get_event_loop().time()
    info = await engine.refresh_runtimes()
    assert asyncio.get_event_loop().time() - t0 < 2            # concurrent, bounded by the timeout
    by = {r["id"]: r for r in info}
    assert by["ok"]["available"] and by["ok"]["models"][0]["id"] == "fake-1" and seen["servers"] == [{"id": "lm"}]
    assert not by["slow"]["available"] and "время" in by["slow"]["error"]
    assert not by["boom"]["available"] and "detect exploded" in by["boom"]["error"]
    assert engine.runtimes_info() == info
    t, data = await asyncio.wait_for(sub.__anext__(), 1)
    assert t == "runtimes" and data["runtimes"] == info
    sub.close()
    await engine.stop()
