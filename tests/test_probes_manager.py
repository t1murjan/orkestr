import asyncio
import time

from orkestr.probes.manager import ProbeManager
from orkestr.probes.servers import make_status


class FakeStore:
    def __init__(self, servers):
        self.servers = {s["id"]: dict(s, status=s.get("status")) for s in servers}
        self.samples = []
        self.saved = []

    def list_servers(self):
        return [dict(s) for s in self.servers.values()]

    def get_server(self, sid):
        s = self.servers.get(sid)
        return dict(s) if s else None

    def save_server_status(self, sid, status):
        if sid not in self.servers:
            return None
        self.servers[sid]["status"] = status
        self.saved.append((sid, status["state"], time.monotonic()))
        return dict(self.servers[sid])

    def add_hardware_sample(self, sample):
        self.samples.append(sample)


def S(id, kind="lmstudio", url="http://x", enabled=True):
    return {"id": id, "name": id, "kind": kind, "base_url": url, "enabled": enabled, "builtin": False}


class StubProber:
    def __init__(self):
        self.calls = []
        self.up = {}
        self.raise_for = set()
        self.delay = {}

    async def probe(self, server):
        self.calls.append((server["id"], time.monotonic()))
        if self.delay.get(server["id"]):
            await asyncio.sleep(self.delay[server["id"]])
        if server["id"] in self.raise_for:
            raise RuntimeError("boom")
        return make_status("up" if self.up.get(server["id"], True) else "down",
                           error=None if self.up.get(server["id"], True) else "нет")

    async def aclose(self):
        pass


class StubCollector:
    def __init__(self):
        self.n = 0
        self.fail_once = True

    async def prime(self):
        pass

    async def sample(self):
        self.n += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("hw boom")
        return {"ts": self.n, "cpu_percent": 1.0}


def mk(store, prober, **kw):
    return ProbeManager(store, hardware_interval_s=0.02, server_interval_s=0.05,
                        collector=StubCollector(), prober=prober, **kw)


async def until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_hardware_loop_survives_error_and_stores():
    st, pr = FakeStore([]), StubProber()
    m = mk(st, pr)
    await m.start()
    assert await until(lambda: len(st.samples) >= 3)
    assert m.latest_hardware() == st.samples[-1]
    await m.stop()


async def test_servers_probed_concurrently_and_exceptions_survive():
    st, pr = FakeStore([S("a"), S("b"), S("c", enabled=False)]), StubProber()
    pr.raise_for.add("b")
    pr.delay["a"] = 0.3
    m = mk(st, pr)
    await m.start()
    assert await until(lambda: any(x[0] == "b" for x in st.saved))
    # b is not blocked by slow a
    assert not any(x[0] == "a" for x in st.saved)
    assert await until(lambda: any(x[0] == "a" for x in st.saved))
    assert not any(x[0] == "c" for x in st.saved)  # disabled
    assert st.servers["b"]["status"]["state"] == "down" and st.servers["b"]["status"]["error"]
    await m.stop()


async def test_backoff_and_check_server_resets():
    st, pr = FakeStore([S("a")]), StubProber()
    pr.up["a"] = False
    m = mk(st, pr, max_backoff_s=0.4)
    await m.start()
    await asyncio.sleep(1.3)
    times = [t for sid, t in pr.calls if sid == "a"]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) >= 4 and gaps[-1] > gaps[0] * 1.5 and max(gaps) < 0.7  # grows, capped
    # healthy again, but backoff pending: explicit check is immediate
    pr.up["a"] = True
    n = len(pr.calls)
    srv = await m.check_server("a")
    assert len(pr.calls) == n + 1 and srv["status"]["state"] == "up"
    # after success interval is back to normal (0.05s)
    t0 = len(pr.calls)
    await asyncio.sleep(0.4)
    assert len(pr.calls) - t0 >= 3
    assert await m.check_server("nope") is None
    await m.stop()


async def test_runtime_changes_added_edited_disabled_removed():
    st, pr = FakeStore([]), StubProber()
    m = mk(st, pr)
    await m.start()
    await asyncio.sleep(0.15)
    assert pr.calls == []
    st.servers["n"] = S("n")
    assert await until(lambda: any(c[0] == "n" for c in pr.calls))
    st.servers["n"]["enabled"] = False
    await asyncio.sleep(0.12)
    k = len(pr.calls)
    await asyncio.sleep(0.3)
    assert len(pr.calls) == k
    st.servers["n"]["enabled"] = True
    assert await until(lambda: len(pr.calls) > k)
    # edit of base_url after failures -> immediate re-probe despite backoff
    pr.up["n"] = False
    assert await until(lambda: m._sched["n"].fails >= 3)
    k = len(pr.calls)
    st.servers["n"]["base_url"] = "http://y"
    assert await until(lambda: len(pr.calls) > k, timeout=0.3)
    del st.servers["n"]
    await asyncio.sleep(0.15)
    assert "n" not in m._sched
    await m.stop()


async def test_local_servers_up():
    up = {"state": "up"}
    st = FakeStore([
        dict(S("l1", "lmstudio"), status=up), dict(S("l2", "llamacpp"), status={"state": "down"}),
        dict(S("a1", "anthropic"), status=up), dict(S("o1", "openai_compat"), status=up),
        dict(S("l3", "lmstudio", enabled=False), status=up), S("l4"),
    ])
    m = mk(st, StubProber())
    assert sorted(s["id"] for s in m.local_servers_up()) == ["l1", "o1"]


async def test_stop_is_fast_with_hung_probe_and_idempotent():
    st, pr = FakeStore([S("a")]), StubProber()
    pr.delay["a"] = 30
    m = mk(st, pr)
    await m.start()
    await asyncio.sleep(0.15)
    t0 = time.monotonic()
    await m.stop()
    assert time.monotonic() - t0 < 0.5
    await m.stop()
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("probe")]


async def test_real_prober_end_to_end_with_fakes():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent / "tools"))
    import fake_model_servers as fk
    f = fk.FakeServer(fk.llamacpp_routes("full")).start()
    st = FakeStore([S("ll", "llamacpp", f.url)])
    m = ProbeManager(st, hardware_interval_s=0.05, server_interval_s=0.1)
    await m.start()
    try:
        assert await until(lambda: st.servers["ll"]["status"] is not None and st.samples, timeout=5)
        assert st.servers["ll"]["status"]["state"] == "up"
        assert [s["id"] for s in m.local_servers_up()] == ["ll"]
        assert st.samples[0]["ram_total_bytes"] > 0
    finally:
        await m.stop()
        f.stop()
