"""In-memory test doubles that follow interfaces.Store / RuntimeAdapter faithfully."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import time
from typing import Any, AsyncIterator, Callable

from orkestr.bus import EventBus
from orkestr.interfaces import StoreError
from orkestr.runtimes.base import (
    ModelInfo,
    RunSpec,
    RuntimeAdapter,
    RuntimeEvent,
    RuntimeInfo,
)


def now_ms() -> int:
    return int(time.time() * 1000)


class FakeStore:
    """Dict backed Store. Publishes the SSE events the real store publishes."""

    def __init__(self, bus: EventBus | None = None, default_cwd: str | None = None) -> None:
        self.bus = bus or EventBus()
        self._ids = itertools.count(1)
        self.settings: dict[str, Any] = {
            "max_depth": 3,
            "max_concurrent_runs": 4,
            "delegate_wait_s": 120,
            "run_timeout_s": 1800,
            "default_cwd": default_cwd or os.getcwd(),
            "watch_external": True,
            "demo_auto": False,
        }
        self.agents: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.steps: dict[str, dict[str, Any]] = {}
        self.step_updates: dict[str, int] = {}
        self.step_update_log: list[tuple[str, dict[str, Any]]] = []

    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):04d}"

    # settings
    def get_settings(self) -> dict[str, Any]:
        return dict(self.settings)

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.settings.update(patch)
        self.bus.publish("settings", dict(self.settings))
        return dict(self.settings)

    # agents
    def _live(self, a: dict[str, Any]) -> dict[str, Any]:
        runs = [r for r in self.runs.values() if r["agent_id"] == a["id"]]
        active = [r for r in runs if r["status"] == "running"]
        status = "idle"
        if active:
            status = "working"
            for r in active:
                for s in self.steps.values():
                    if s["run_id"] == r["id"] and s["kind"] == "delegation" and s["status"] == "running":
                        status = "delegating"
        done = sorted((r for r in runs if r["status"] in ("ok", "error", "cancelled")), key=lambda r: r["ended_at"] or 0)
        if not active and done and done[-1]["status"] == "error":
            status = "error"
        cur = active[-1] if active else None
        return {
            "status": status,
            "active_runs": len(active),
            "current_run_id": cur["id"] if cur else None,
            "activity": cur["activity"] if cur else None,
            "context_used": None, "context_window": None, "model": None,
            "tokens_in": sum(r["tokens_in"] for r in runs),
            "tokens_out": sum(r["tokens_out"] for r in runs),
            "cost_usd": sum(r["cost_usd"] for r in runs),
            "runs_total": len(runs),
            "last_run_at": None,
            "last_status": done[-1]["status"] if done else None,
        }

    def _agent_view(self, a: dict[str, Any]) -> dict[str, Any]:
        d = dict(a)
        d["live"] = self._live(a)
        return d

    def list_agents(self) -> list[dict[str, Any]]:
        return [self._agent_view(a) for a in self.agents.values()]

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        a = self.agents.get(agent_id)
        return self._agent_view(a) if a else None

    def get_agent_by_name(self, name: str) -> dict[str, Any] | None:
        for a in self.agents.values():
            if a["name"] == name:
                return self._agent_view(a)
        return None

    def create_agent(self, data: dict[str, Any]) -> dict[str, Any]:
        if any(a["name"] == data["name"] for a in self.agents.values()):
            raise StoreError("name_taken", "Имя занято")
        t = now_ms()
        a = {
            "id": self._id("a"), "name": data["name"], "runtime": data["runtime"],
            "model": data.get("model"), "role": data.get("role", ""),
            "system_prompt": data.get("system_prompt", ""), "cwd": data.get("cwd"),
            "permissions": data.get("permissions", "edit"), "color": data.get("color"),
            "x": data.get("x", 0), "y": data.get("y", 0), "demo": bool(data.get("demo", False)),
            "created_at": t, "updated_at": t,
        }
        self.agents[a["id"]] = a
        self.bus.publish("agent", self._agent_view(a))
        return self._agent_view(a)

    def update_agent(self, agent_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        a = self.agents.get(agent_id)
        if not a:
            return None
        a.update(patch)
        a["updated_at"] = now_ms()
        self.bus.publish("agent", self._agent_view(a))
        return self._agent_view(a)

    def delete_agent(self, agent_id: str) -> bool:
        if agent_id not in self.agents:
            return False
        del self.agents[agent_id]
        for eid in [e["id"] for e in self.edges.values() if agent_id in (e["supervisor_id"], e["subordinate_id"])]:
            self.delete_edge(eid)
        self.bus.publish("agent.deleted", {"id": agent_id})
        return True

    # hierarchy
    def list_edges(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self.edges.values()]

    def _reaches(self, src: str, dst: str) -> bool:
        stack, seen = [src], set()
        while stack:
            cur = stack.pop()
            if cur == dst:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(e["subordinate_id"] for e in self.edges.values() if e["supervisor_id"] == cur)
        return False

    def create_edge(self, supervisor_id: str, subordinate_id: str) -> dict[str, Any]:
        if supervisor_id not in self.agents or subordinate_id not in self.agents:
            raise StoreError("unknown_agent", "Нет такого агента")
        if supervisor_id == subordinate_id:
            raise StoreError("self_edge", "Нельзя связать агента с самим собой")
        if any(e["supervisor_id"] == supervisor_id and e["subordinate_id"] == subordinate_id for e in self.edges.values()):
            raise StoreError("duplicate_edge", "Связь уже есть")
        if self._reaches(subordinate_id, supervisor_id):
            raise StoreError("cycle", "Цикл")
        e = {"id": self._id("e"), "supervisor_id": supervisor_id, "subordinate_id": subordinate_id, "created_at": now_ms()}
        self.edges[e["id"]] = e
        self.bus.publish("edge", dict(e))
        return dict(e)

    def delete_edge(self, edge_id: str) -> bool:
        if edge_id not in self.edges:
            return False
        del self.edges[edge_id]
        self.bus.publish("edge.deleted", {"id": edge_id})
        return True

    def subordinates_of(self, agent_id: str) -> list[dict[str, Any]]:
        return [self._agent_view(self.agents[e["subordinate_id"]]) for e in self.edges.values()
                if e["supervisor_id"] == agent_id and e["subordinate_id"] in self.agents]

    def supervisors_of(self, agent_id: str) -> list[dict[str, Any]]:
        return [self._agent_view(self.agents[e["supervisor_id"]]) for e in self.edges.values()
                if e["subordinate_id"] == agent_id and e["supervisor_id"] in self.agents]

    # runs
    def create_run(self, data: dict[str, Any]) -> dict[str, Any]:
        r = dict(data)
        r.setdefault("id", self._id("r"))
        r.setdefault("root_run_id", r["id"])
        self.runs[r["id"]] = r
        self.bus.publish("run", dict(r))
        return dict(r)

    def update_run(self, run_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        r = self.runs.get(run_id)
        if not r:
            return None
        r.update(patch)
        self.bus.publish("run", dict(r))
        return dict(r)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        r = self.runs.get(run_id)
        return dict(r) if r else None

    def list_runs(self, *, status: str | None = None, agent_id: str | None = None, origin: str | None = None,
                  root_only: bool = False, limit: int = 50, before: int | None = None) -> list[dict[str, Any]]:
        sts = set(status.split(",")) if status else None
        out = []
        for r in self.runs.values():
            if sts and r["status"] not in sts:
                continue
            if agent_id and r["agent_id"] != agent_id:
                continue
            if origin and r["origin"] != origin:
                continue
            if root_only and r["parent_run_id"]:
                continue
            if before is not None and not (r["started_at"] < before):
                continue
            out.append(dict(r))
        out.sort(key=lambda r: r["started_at"], reverse=True)
        return out[:limit]

    def run_tree(self, root_run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.runs.values() if r["root_run_id"] == root_run_id]

    # steps
    def create_step(self, data: dict[str, Any]) -> dict[str, Any]:
        s = dict(data)
        s.setdefault("id", self._id("s"))
        self.steps[s["id"]] = s
        self.bus.publish("step", dict(s))
        return dict(s)

    def update_step(self, step_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        s = self.steps.get(step_id)
        if not s:
            return None
        s.update(patch)
        self.step_updates[step_id] = self.step_updates.get(step_id, 0) + 1
        self.step_update_log.append((step_id, dict(patch)))
        self.bus.publish("step", dict(s))
        return dict(s)

    def get_step(self, step_id: str) -> dict[str, Any] | None:
        s = self.steps.get(step_id)
        return dict(s) if s else None

    def list_steps(self, run_ids: list[str]) -> list[dict[str, Any]]:
        ids = set(run_ids)
        out = [dict(s) for s in self.steps.values() if s["run_id"] in ids]
        out.sort(key=lambda s: (s["started_at"], s["seq"]))
        return out

    # unused by the engine
    def list_servers(self): return []
    def get_server(self, server_id): return None
    def create_server(self, data): raise NotImplementedError
    def update_server(self, server_id, patch): return None
    def delete_server(self, server_id): return False
    def save_server_status(self, server_id, status): return None
    def add_hardware_sample(self, sample): pass
    def list_hardware_samples(self, since_ms, max_points=300): return []
    def overview(self, window_s=3600, buckets=60): return {}


Script = Callable[[RunSpec], AsyncIterator[RuntimeEvent]]


class ScriptedAdapter(RuntimeAdapter):
    """Adapter whose run() is a user supplied async generator function.

    Records every spec and whether each generator was closed (finally block ran).
    """

    def __init__(self, script: Script | None = None, adapter_id: str = "demo", available: bool = True) -> None:
        self.id = adapter_id
        self.label = adapter_id
        self.script = script or self.default_script
        self.available = available
        self.specs: list[RunSpec] = []
        self.started = 0
        self.closed = 0
        self.live = 0
        self.max_live = 0

    @staticmethod
    async def default_script(spec: RunSpec):
        yield RuntimeEvent("session", {"session_id": "s-" + spec.run_id, "model": "fake-1", "context_window": 1000})
        yield RuntimeEvent("text", {"text": "готово: " + spec.task, "delta": False})
        yield RuntimeEvent("result", {"text": "готово: " + spec.task, "is_error": False, "cost_usd": None, "num_turns": 1})

    async def detect(self, local_servers=None) -> RuntimeInfo:
        if not self.available:
            return RuntimeInfo(self.id, self.label, False, error="нет CLI")
        return RuntimeInfo(self.id, self.label, True, version="1", models=[ModelInfo("fake-1", "Fake", "fake", "local", 1000)])

    async def run(self, spec: RunSpec):
        self.specs.append(spec)
        self.started += 1
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        try:
            async with contextlib.aclosing(self.script(spec)) as inner:
                async for ev in inner:
                    yield ev
        finally:
            self.live -= 1
            self.closed += 1


async def wait_until(pred: Callable[[], bool], timeout: float = 5.0, step: float = 0.01) -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(step)


# ---------------------------------------------------------------------------
# shared builders for the engine / mcp tests
# ---------------------------------------------------------------------------
from orkestr.engine import RunEngine  # noqa: E402


def make_store(kind: str, tmp_path, bus: EventBus):
    if kind == "sqlite":
        from orkestr.store import SqliteStore

        st = SqliteStore(str(tmp_path / "t.db"), bus)
        st.update_settings({"default_cwd": str(tmp_path)})
        return st
    return FakeStore(bus, default_cwd=str(tmp_path))


class Env:
    """store + bus + engine around one ScriptedAdapter registered as "demo"."""

    def __init__(self, store, bus, engine, adapter) -> None:
        self.store, self.bus, self.engine, self.adapter = store, bus, engine, adapter

    def agent(self, name: str, **kw):
        data = {"name": name, "runtime": "demo", "role": kw.pop("role", f"роль {name}")}
        data.update(kw)
        return self.store.create_agent(data)

    def link(self, sup, sub):
        return self.store.create_edge(sup["id"], sub["id"])

    def steps(self, run_id: str):
        return self.store.list_steps([run_id])


async def make_env(kind, tmp_path, script=None, *, adapter=None, probes=None, **settings) -> Env:
    bus = EventBus()
    store = make_store(kind, tmp_path, bus)
    if settings:
        store.update_settings(settings)
    adapter = adapter or ScriptedAdapter(script)
    engine = RunEngine(store, bus, {adapter.id: adapter}, probes, base_url="http://127.0.0.1:9999")
    await engine.start()
    return Env(store, bus, engine, adapter)
