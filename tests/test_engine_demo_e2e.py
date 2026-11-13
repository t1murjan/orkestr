"""Engine + MCP + DemoAdapter end to end over real HTTP (the scenario of tests/tools/e2e_demo.py)."""
from __future__ import annotations

import socket

import pytest

from tests.tools.e2e_demo import run_e2e



def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("round_", [1, 2, 3])
async def test_demo_team_delegates_end_to_end(round_):
    out = await run_e2e(port=free_port(), speed=60, fail_rate=0.0, quiet=True, timeout=60)
    root, tree, steps = out["root"], out["tree"], out["steps"]
    assert root["status"] == "ok" and root["delegations"] >= 1
    children = [r for r in tree if r["parent_run_id"]]
    assert len(children) >= 1
    assert all(r["status"] == "ok" for r in tree)
    deleg_steps = [s for s in steps if s["kind"] == "delegation"]
    assert len(deleg_steps) == len(children)
    for s in deleg_steps:
        child = next(r for r in tree if r["id"] == s["child_run_id"])        # every delegation step is linked
        assert child["parent_step_id"] == s["id"] and child["origin"] == "delegation"
        assert child["root_run_id"] == root["id"]
        assert s["status"] == "ok" and s["output"]
    assert all(s["status"] != "running" for s in steps)
    assert all(r["tokens_in"] > 0 and r["context_used"] for r in tree)
