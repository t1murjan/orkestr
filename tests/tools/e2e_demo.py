"""End-to-end demo: engine + MCP app + DemoAdapter + seeded team, one root task, prints the run tree.

    .venv/bin/python tests/tools/e2e_demo.py [--port 8802] [--speed 8] [--fail-rate 0] [--task "..."]

Boots a real HTTP server (uvicorn) with the MCP app mounted at /mcp, so the demo agent's delegation
goes through the real endpoint. Uses a temporary sqlite database; stops everything on exit.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Mount  # noqa: E402

from orkestr.bus import EventBus  # noqa: E402
from orkestr.demo import LEAD_NAME, seed_demo  # noqa: E402
from orkestr.engine import RunEngine  # noqa: E402
from orkestr.mcp_server import build_mcp_app  # noqa: E402
from orkestr.runtimes.demo import DemoAdapter  # noqa: E402
from orkestr.store import SqliteStore  # noqa: E402

DEFAULT_TASK = "Добавить в сервис заказов эндпоинт экспорта в CSV и покрыть его тестами"


async def run_e2e(port: int = 8802, speed: float = 8.0, fail_rate: float = 0.0, task: str = DEFAULT_TASK,
                  timeout: float = 180.0, quiet: bool = False) -> dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="orkestr-e2e-")
    bus = EventBus()
    store = SqliteStore(os.path.join(tmp, "e2e.db"), bus)
    store.update_settings({"default_cwd": tmp})
    adapter = DemoAdapter(speed=speed, fail_rate=fail_rate)
    engine = RunEngine(store, bus, {"demo": adapter}, base_url=f"http://127.0.0.1:{port}")
    app = Starlette(routes=[Mount("/mcp", app=build_mcp_app(engine, store))])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off",
                                           timeout_graceful_shutdown=2))
    srv = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        await engine.start()
        await engine.refresh_runtimes()
        seeded = seed_demo(store)
        assert seed_demo(store)["agents"].__len__() == len(seeded["agents"])      # idempotent
        lead = store.get_agent_by_name(LEAD_NAME)
        run = await engine.start_run(lead["id"], task)
        final = await engine.wait_run(run["id"], timeout)
        tree = store.run_tree(run["id"])
        steps = store.list_steps([r["id"] for r in tree])
        if not quiet:
            print_tree(tree, steps, run["id"])
        return {"root": final, "tree": tree, "steps": steps}
    finally:
        await engine.stop()
        server.should_exit = True
        await srv
        store.close()


def print_tree(tree: list[dict[str, Any]], steps: list[dict[str, Any]], root_id: str) -> None:
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for r in tree:
        by_parent.setdefault(r["parent_run_id"], []).append(r)
    by_run: dict[str, list[dict[str, Any]]] = {}
    for s in steps:
        by_run.setdefault(s["run_id"], []).append(s)

    def show(run: dict[str, Any], indent: int) -> None:
        pad = "    " * indent
        dur = f"{(run['duration_ms'] or 0) / 1000:.1f}s"
        print(f"{pad}[{run['status']}] {run['agent_name']} ({run['model']}) depth={run['depth']} {dur} "
              f"tok={run['tokens_in']}/{run['tokens_out']} ctx={run['context_used']}/{run['context_window']} "
              f"tools={run['tool_calls']} deleg={run['delegations']} origin={run['origin']}")
        print(f"{pad}  task: {run['task'][:100]}")
        for s in by_run.get(run["id"], []):
            extra = f" -> child {s['child_run_id']}" if s["child_run_id"] else ""
            out = (s["output"] or "").replace("\n", " ")[:70]
            print(f"{pad}  #{s['seq']} {s['kind']:<10} {s['status']:<5} {s['title'][:48]:<48}{extra}  {out}")
            if s["kind"] == "delegation" and s["child_run_id"]:
                child = next(r for r in tree if r["id"] == s["child_run_id"])
                show(child, indent + 1)
        if run["result"] or run["error"]:
            print(f"{pad}  => {(run['result'] or run['error'])[:110]!r}")

    show(next(r for r in tree if r["id"] == root_id), 0)
    unlinked = [r for r in tree if r["parent_run_id"] and not r["parent_step_id"]]
    print(f"\nruns in tree: {len(tree)}, steps: {len(steps)}, delegated runs without a linked step: {len(unlinked)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8802)
    ap.add_argument("--speed", type=float, default=8.0)
    ap.add_argument("--fail-rate", type=float, default=0.0)
    ap.add_argument("--task", default=DEFAULT_TASK)
    a = ap.parse_args()
    asyncio.run(run_e2e(a.port, a.speed, a.fail_rate, a.task))


if __name__ == "__main__":
    main()
