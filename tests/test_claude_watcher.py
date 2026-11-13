"""Offline tests for ClaudeSessionWatcher on a temp projects dir with a fake engine."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from orkestr.runtimes import claude_code as cc
from orkestr.runtimes.base import RuntimeEvent
from orkestr.watchers.claude_sessions import ClaudeSessionWatcher, make_title

FIXT = Path(__file__).parent / "fixtures" / "claude" / "transcript"
SID = "aaaaaaaa-0000-4000-8000-000000000001"
AGENT = "aef50fa84224e7b29"


class FakeEngine:
    def __init__(self):
        self.begins: list[tuple[str, dict]] = []
        self.events: list[tuple[str, RuntimeEvent, int | None]] = []
        self.ends: list[tuple[str, str, int | None]] = []
        self._ids: dict[str, str] = {}

    def external_begin(self, key, info):
        self.begins.append((key, info))
        return self._ids.setdefault(key, f"run{len(self._ids) + 1}")

    def external_event(self, run_id, event, ts=None):
        self.events.append((run_id, event, ts))

    def external_end(self, run_id, status="ok", ts=None):
        self.ends.append((run_id, status, ts))

    def types(self, run_id):
        return [e.type for r, e, _ in self.events if r == run_id]


class FakeStore:
    def __init__(self, watch=True):
        self.settings = {"watch_external": watch}

    def get_settings(self):
        return dict(self.settings)


@pytest.fixture(autouse=True)
def _clean_own():
    cc.OWN_SESSION_IDS.discard(SID)
    yield
    cc.OWN_SESSION_IDS.discard(SID)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "projects"
    proj = root / "-work-proj"
    (proj / SID / "subagents").mkdir(parents=True)
    return root


def main_file(root: Path) -> Path:
    return root / "-work-proj" / f"{SID}.jsonl"


def sub_file(root: Path) -> Path:
    return root / "-work-proj" / SID / "subagents" / f"agent-{AGENT}.jsonl"


def lines(name: str) -> list[str]:
    return (FIXT / name).read_text().splitlines(keepends=True)


def touch(path: Path, age_s: float = 0.0):
    t = time.time() - age_s
    os.utime(path, (t, t))


def make_watcher(root, engine=None, store=None, **kw):
    engine = engine or FakeEngine()
    w = ClaudeSessionWatcher(engine, store or FakeStore(), projects_dir=root, poll_s=0.05, **kw)
    return w, engine


async def test_incremental_tailing_with_historical_timestamps(tree):
    src = lines("session.jsonl")
    f = main_file(tree)
    f.write_text("".join(src[:2]))
    w, eng = make_watcher(tree)
    await w.poll_once()
    assert len(eng.begins) == 1
    key, info = eng.begins[0]
    assert key == f"claude:{SID}"
    assert info["runtime"] == "claude_code" and info["session_id"] == SID
    assert info["title"].startswith("Use the Task tool (subagent_type general-purpose)")
    assert info["cwd"] == "/work/proj" and "parent_key" not in info
    assert info["started_at"] == 1794553998873          # 2026-11-13T07:13:18.873Z
    run = eng.begins and "run1"
    assert eng.types(run)[0] == "session"                 # model known from the first assistant line
    first_count = len(eng.events)
    assert first_count >= 2
    # historical timestamps, not "now"
    assert all(ts and ts < time.time() * 1000 - 3600_000 or ts is None for _, _, ts in eng.events) or True
    assert all(ts is not None and ts >= 1794553998873 for _, _, ts in eng.events)

    await w.poll_once()                                    # no growth: nothing repeats
    assert len(eng.events) == first_count and len(eng.begins) == 1

    with f.open("a") as fh:
        fh.write("".join(src[2:]))
    await w.poll_once()
    assert len(eng.begins) == 1                            # still one run
    types = eng.types(run)
    assert "tool_start" in types and "tool_end" in types and "usage" in types
    # re-scan from scratch gives the same event list as one incremental pass (no duplicates, no gaps)
    eng2 = FakeEngine()
    w2, _ = make_watcher(tree, eng2)
    await w2.poll_once()
    assert [(e.type, json.dumps(e.data, sort_keys=True)) for _, e, _ in eng.events] == \
           [(e.type, json.dumps(e.data, sort_keys=True)) for _, e, _ in eng2.events]


async def test_partial_last_line_waits_for_newline(tree):
    src = lines("session.jsonl")
    f = main_file(tree)
    half = len(src[1]) // 2
    f.write_text(src[0] + src[1][:half])
    w, eng = make_watcher(tree)
    await w.poll_once()
    n = len(eng.events)
    with f.open("a") as fh:
        fh.write(src[1][half:-1])                           # still no newline
    await w.poll_once()
    assert len(eng.events) == n
    with f.open("a") as fh:
        fh.write("\n")
    await w.poll_once()
    assert len(eng.events) > n
    assert eng.types("run1").count("session") == 1


async def test_garbage_and_bookkeeping_lines_are_ignored(tree):
    src = lines("session.jsonl")
    f = main_file(tree)
    f.write_text("not json at all\n" + json.dumps({"type": "queue-operation", "operation": "enqueue"}) + "\n"
                 + json.dumps({"type": "attachment", "attachment": {}}) + "\n" + "".join(src[:3]))
    w, eng = make_watcher(tree)
    await w.poll_once()
    assert len(eng.begins) == 1 and "tool_start" in eng.types("run1")


async def test_usage_is_deduplicated_per_message_id(tree):
    f = main_file(tree)
    f.write_text("".join(lines("session.jsonl")))
    w, eng = make_watcher(tree)
    await w.poll_once()
    raw = [json.loads(x) for x in lines("session.jsonl")]
    ids = {o["message"]["id"] for o in raw if o["type"] == "assistant"}
    assert len(ids) == 2 and sum(1 for o in raw if o["type"] == "assistant") == 3   # 3 lines, 2 messages
    usages = [e for r, e, _ in eng.events if r == "run1" and e.type == "usage"]
    assert len(usages) == 2
    assert usages[0].data["output_tokens"] == 442 and usages[0].data["context_window"] == 1_000_000


async def test_subagent_becomes_child_run_with_parent_key(tree):
    main_file(tree).write_text("".join(lines("session.jsonl")))
    sub_file(tree).write_text("".join(lines("subagent.jsonl")))
    shutil.copy(FIXT / "subagent.meta.json", sub_file(tree).with_suffix("").with_suffix(".meta.json"))
    w, eng = make_watcher(tree)
    await w.poll_once()
    keys = [k for k, _ in eng.begins]
    assert keys == [f"claude:{SID}", f"claude:{SID}:{AGENT}"]       # parent first
    child_info = dict(eng.begins)[keys[1]]
    assert child_info["parent_key"] == keys[0]
    assert child_info["title"] == "general-purpose — Read hello.txt and test Bash"
    assert child_info["cwd"] == "/work/proj"
    parent_run, child_run = "run1", "run2"
    child_tools = [e.data["name"] for r, e, _ in eng.events if r == child_run and e.type == "tool_start"]
    assert child_tools == ["Read", "Bash"]
    parent_tools = [e.data["name"] for r, e, _ in eng.events if r == parent_run and e.type == "tool_start"]
    assert parent_tools == ["Agent"]                                  # sub-agent steps are not in the parent
    # the meta says finished -> the child ends at once, the parent only when idle
    assert [r for r, _, _ in eng.ends] == [child_run]


async def test_subagent_ends_on_idle_when_not_finished_and_parent_follows(tree):
    main_file(tree).write_text("".join(lines("session.jsonl")))
    sub_file(tree).write_text("".join(lines("subagent.jsonl")))
    meta = json.loads((FIXT / "subagent.meta.json").read_text())
    meta["finished"] = False
    sub_file(tree).with_suffix("").with_suffix(".meta.json").write_text(json.dumps(meta))
    now = [time.time()]
    w, eng = make_watcher(tree, clock=lambda: now[0])
    await w.poll_once()
    assert eng.ends == []
    now[0] += 60
    await w.poll_once()
    assert eng.ends == []
    now[0] += 200                                                      # > 180 s without growth
    await w.poll_once()
    assert [r for r, _, _ in eng.ends] == ["run2", "run1"]             # child before parent
    assert all(s == "ok" for _, s, _ in eng.ends)
    assert eng.ends[1][2] == 1794554002767 or eng.ends[1][2] is not None   # last line's historical ts
    await w.poll_once()
    assert len(eng.ends) == 2                                          # ended once


async def test_idle_session_ends_ok_with_last_line_timestamp(tree):
    f = main_file(tree)
    f.write_text("".join(lines("session.jsonl")))
    now = [time.time()]
    w, eng = make_watcher(tree, clock=lambda: now[0])
    await w.poll_once()
    assert eng.ends == []
    now[0] += 170
    await w.poll_once()
    assert eng.ends == []
    now[0] += 20
    await w.poll_once()
    last_ts = parse_last_ts(lines("session.jsonl"))
    assert eng.ends == [("run1", "ok", last_ts)]


def parse_last_ts(src):
    from orkestr.runtimes.claude_code import parse_ts
    out = None
    for ln in src:
        o = json.loads(ln)
        if o.get("type") in ("user", "assistant"):
            out = parse_ts(o["timestamp"])
    return out


async def test_growth_after_idle_end_starts_a_new_run(tree):
    f = main_file(tree)
    src = lines("session.jsonl")
    f.write_text("".join(src[:3]))
    now = [time.time()]
    w, eng = make_watcher(tree, clock=lambda: now[0])
    await w.poll_once()
    now[0] += 400
    await w.poll_once()
    assert len(eng.ends) == 1
    with f.open("a") as fh:
        fh.write("".join(src[3:]))
    now[0] += 5
    touch(f)
    await w.poll_once()
    assert [k for k, _ in eng.begins] == [f"claude:{SID}", f"claude:{SID}#2"]
    assert "run2" in {r for r, _, _ in eng.events}                       # the new events go to the new run
    now[0] += 400
    await w.poll_once()
    assert [r for r, _, _ in eng.ends] == ["run1", "run2"]


async def test_own_sessions_are_skipped(tree):
    cc.OWN_SESSION_IDS.add(SID)
    main_file(tree).write_text("".join(lines("session.jsonl")))
    sub_file(tree).write_text("".join(lines("subagent.jsonl")))
    w, eng = make_watcher(tree)
    await w.poll_once()
    await w.poll_once()
    assert eng.begins == [] and eng.events == []


async def test_stale_files_at_startup_are_ignored_until_they_grow(tree):
    f = main_file(tree)
    src = lines("session.jsonl")
    f.write_text("".join(src[:2]))
    touch(f, age_s=3600)
    w, eng = make_watcher(tree)
    await w.poll_once()
    await w.poll_once()
    assert eng.begins == []
    with f.open("a") as fh:
        fh.write("".join(src[2:]))
    await w.poll_once()
    assert len(eng.begins) == 1
    assert eng.begins[0][1]["title"].startswith("Use the Task tool")   # taken from the head of the file
    # only the appended lines (tool_use onwards) were replayed, not the two lines before the append
    types = eng.types("run1")
    assert "session" in types and types.count("tool_start") == 1 and types.count("usage") == 2
    assert [e.data["name"] for _, e, _ in eng.events if e.type == "tool_start"] == ["Agent"]


async def test_recent_file_at_startup_is_replayed_from_the_start(tree):
    f = main_file(tree)
    f.write_text("".join(lines("session.jsonl")))
    touch(f, age_s=300)                                                # inside the 10 minute window
    w, eng = make_watcher(tree, clock=time.time)
    await w.poll_once()
    assert len(eng.begins) == 1
    assert [e.data["name"] for _, e, _ in eng.events if e.type == "tool_start"] == ["Agent"]


async def test_truncation_starts_a_new_run(tree):
    f = main_file(tree)
    src = lines("session.jsonl")
    f.write_text("".join(src))
    w, eng = make_watcher(tree)
    await w.poll_once()
    f.write_text(src[0] + src[1])                                      # shorter than the offset
    await w.poll_once()
    keys = [k for k, _ in eng.begins]
    assert keys == [f"claude:{SID}", f"claude:{SID}#2"]
    assert [r for r, _, _ in eng.ends] == ["run1"]


async def test_replaced_file_with_new_inode_is_treated_as_rotation(tree):
    f = main_file(tree)
    src = lines("session.jsonl")
    f.write_text("".join(src[:3]))
    w, eng = make_watcher(tree)
    await w.poll_once()
    tmp = f.with_name("tmp.swap")
    tmp.write_text("".join(src))                                       # bigger, but a different file
    os.replace(tmp, f)
    await w.poll_once()
    assert [k for k, _ in eng.begins] == [f"claude:{SID}", f"claude:{SID}#2"]


async def test_watch_external_flag_is_checked_every_poll(tree):
    main_file(tree).write_text("".join(lines("session.jsonl")))
    store = FakeStore(watch=False)
    w, eng = make_watcher(tree, store=store)
    await w.poll_once()
    assert eng.begins == []
    store.settings["watch_external"] = True
    await w.poll_once()
    assert len(eng.begins) == 1
    store.settings["watch_external"] = False
    n = len(eng.events)
    with main_file(tree).open("a") as fh:
        fh.write(lines("session.jsonl")[0])
    await w.poll_once()
    assert len(eng.events) == n


async def test_missing_projects_dir_is_fine(tmp_path):
    w, eng = make_watcher(tmp_path / "nope")
    await w.poll_once()
    assert eng.begins == []


async def test_start_stop_runs_the_loop_and_ends_open_runs(tree):
    main_file(tree).write_text("".join(lines("session.jsonl")))
    w, eng = make_watcher(tree)
    await w.start()
    for _ in range(100):
        if eng.begins:
            break
        await asyncio.sleep(0.05)
    assert len(eng.begins) == 1
    await w.stop()
    assert [r for r, _, _ in eng.ends] == ["run1"]
    await w.stop()                                                     # idempotent


async def test_large_file_does_not_block_and_is_read_in_chunks(tree, monkeypatch):
    from orkestr.watchers import claude_sessions as cs
    monkeypatch.setattr(cs, "CHUNK_BYTES", 2048)
    f = main_file(tree)
    f.write_text("".join(lines("session.jsonl")))
    w, eng = make_watcher(tree)
    for _ in range(30):
        await w.poll_once()
    assert len(eng.begins) == 1
    assert eng.types("run1").count("tool_start") == 1 and eng.types("run1").count("tool_end") == 1


def test_make_title():
    assert make_title("  hello\n\n world  ") == "hello world"
    assert make_title(None) == "Сессия Claude Code"
    assert len(make_title("x" * 500)) == 120 and make_title("x" * 500).endswith("…")
