"""Read-only observer of Claude Code sessions started in a terminal.

Verified on this machine (Claude Code 2.1.323) by looking at what my own runs wrote:

Layout under ``~/.claude/projects/``::

    <project>/<session-uuid>.jsonl                          main transcript
    <project>/<session-uuid>/subagents/agent-<id>.jsonl     sub-agent transcript
    <project>/<session-uuid>/subagents/agent-<id>.meta.json {agentType, description,
                                                            toolUseId, finished, ...}

``<project>`` is the cwd with every non-alphanumeric character turned into "-".
Sub-agents are NOT inlined in the main file; the main file only holds the
``Agent`` tool_use / tool_result. Lines are JSON objects with a ``type``:
``user`` / ``assistant`` (``message`` as in the API, ``timestamp`` ISO-8601 Z,
``sessionId``, ``cwd``, ``isSidechain``, ``uuid``, ``parentUuid``), plus
bookkeeping lines (``attachment``, ``queue-operation``, ``last-prompt``,
``api-request*``, ``cost-state`` ...) that are ignored. One API message is
written as one ``assistant`` line per content block, all with the same
``message.id`` and the final ``usage``. The first real user prompt is a
``user`` line with a string ``content``; tool results are ``user`` lines with
``tool_result`` blocks. Sub-agent files have ``isSidechain: true`` and an
``agentId``; their first line is the prompt given to the sub-agent.

Parsing is shared with the adapter (``StreamTranslator``), so a terminal session
yields the same events as a run launched by Orkestr.

Behaviour
---------
* At startup only files modified in the last ``active_window_s`` (10 min) are
  replayed from byte 0 (historical ``timestamp`` is passed as ``ts``); older ones
  are remembered at their current size and picked up if they grow later (title
  is then read from the head of the file).
* Per file a byte offset is kept; only complete lines (up to the last ``\\n``)
  are consumed, so a half-written last line is picked up on the next poll.
  Truncation or replacement of the file (size < offset or a new inode) closes the
  old run and starts a new one. Reads are capped per poll and run in a thread.
* No file growth for ``idle_end_s`` (3 min) -> ``external_end(status="ok")`` with
  the timestamp of the last line. Growth afterwards starts a new run (key suffix
  ``#2``...). A finished sub-agent (``meta.json`` finished=true) ends at once.
* Sessions whose id is in ``claude_code.OWN_SESSION_IDS`` (started by the adapter)
  are skipped, including their sub-agents.
* ``settings.watch_external`` is checked on every poll; when false nothing is read.
* Sub-agents become child runs through ``parent_key``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..runtimes.base import RuntimeEvent
from ..runtimes.claude_code import (
    OWN_SESSION_IDS,
    StreamTranslator,
    guess_context_window,
    parse_ts,
    user_prompt_text,
)

log = logging.getLogger("orkestr.claude.watcher")

CHUNK_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024 * 1024
PEEK_BYTES = 256 * 1024
TITLE_MAX = 120
DEFAULT_TITLE = "Сессия Claude Code"
_SKIP_PROMPT_PREFIXES = ("<command-name>", "<local-command", "<command-message>", "Caveat:")


def default_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def make_title(text: str | None) -> str:
    if not text:
        return DEFAULT_TITLE
    t = re.sub(r"\s+", " ", text).strip()
    if not t:
        return DEFAULT_TITLE
    return t if len(t) <= TITLE_MAX else t[: TITLE_MAX - 1].rstrip() + "…"


def _is_title_prompt(text: str | None) -> bool:
    return bool(text and text.strip() and not text.lstrip().startswith(_SKIP_PROMPT_PREFIXES))


@dataclass
class _File:
    kind: str                       # "main" | "sub"
    path: str
    session_id: str
    agent_id: str | None = None
    offset: int = 0
    ino: int = 0
    size: int = 0
    mtime: float = 0.0
    gen: int = 1
    run_id: str | None = None
    key: str | None = None
    tr: StreamTranslator = field(default_factory=StreamTranslator)
    title: str | None = None
    cwd: str | None = None
    first_ts: int | None = None
    last_ts: int | None = None
    session_sent: bool = False
    active: bool = False            # a run is open
    dormant: bool = False           # stale at startup: waiting for growth
    meta: dict[str, Any] | None = None

    def base_key(self) -> str:
        return f"claude:{self.session_id}" + (f":{self.agent_id}" if self.agent_id else "")

    def new_run_key(self) -> str:
        return self.base_key() + (f"#{self.gen}" if self.gen > 1 else "")


def _read_chunk(path: str, offset: int, limit: int = CHUNK_BYTES) -> tuple[list[dict[str, Any]], int, int]:
    """Read complete lines from `offset`. Returns (objects, bytes_consumed, file_size)."""
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if offset >= size:
            return [], 0, size
        fh.seek(offset)
        want = limit
        data = fh.read(want)
        cut = data.rfind(b"\n")
        while cut < 0 and len(data) == want and want < MAX_LINE_BYTES:
            want *= 2
            fh.seek(offset)
            data = fh.read(want)
            cut = data.rfind(b"\n")
        if cut < 0:
            if len(data) >= MAX_LINE_BYTES:
                return [], len(data), size           # absurdly long line: skip it
            return [], 0, size                       # partial last line: wait for the rest
        chunk = data[: cut + 1]
    objs: list[dict[str, Any]] = []
    for raw in chunk.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs, len(chunk), size


def _peek_head(path: str) -> dict[str, Any]:
    """Title / cwd / first timestamp from the head of a transcript."""
    info: dict[str, Any] = {"title": None, "cwd": None, "first_ts": None}
    try:
        with open(path, "rb") as fh:
            data = fh.read(PEEK_BYTES)
    except OSError:
        return info
    for raw in data.split(b"\n")[:-1] if b"\n" in data else []:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if info["cwd"] is None and isinstance(obj.get("cwd"), str):
            info["cwd"] = obj["cwd"]
        if info["first_ts"] is None and obj.get("type") in ("user", "assistant"):
            info["first_ts"] = parse_ts(obj.get("timestamp"))
        if info["title"] is None:
            text = user_prompt_text(obj)
            if _is_title_prompt(text):
                info["title"] = make_title(text)
        if info["title"] and info["cwd"] and info["first_ts"]:
            break
    return info


def _scan(root: str) -> list[tuple[str, str, str, str | None, int, float, int]]:
    """(kind, path, session_id, agent_id, size, mtime, inode) for every transcript."""
    out: list[tuple[str, str, str, str | None, int, float, int]] = []
    try:
        projects = list(os.scandir(root))
    except OSError:
        return out
    for proj in projects:
        try:
            if not proj.is_dir(follow_symlinks=False):
                continue
            entries = list(os.scandir(proj.path))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_file(follow_symlinks=False) and e.name.endswith(".jsonl"):
                    st = e.stat()
                    out.append(("main", e.path, e.name[:-6], None, st.st_size, st.st_mtime, st.st_ino))
                elif e.is_dir(follow_symlinks=False):
                    sub = os.path.join(e.path, "subagents")
                    if not os.path.isdir(sub):
                        continue
                    for f in os.scandir(sub):
                        if f.name.startswith("agent-") and f.name.endswith(".jsonl") and f.is_file(follow_symlinks=False):
                            st = f.stat()
                            out.append(("sub", f.path, e.name, f.name[6:-6], st.st_size, st.st_mtime, st.st_ino))
            except OSError:
                continue
    return out


def _read_meta(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


class ClaudeSessionWatcher:
    def __init__(
        self,
        engine: Any,
        store: Any,
        *,
        projects_dir: str | os.PathLike[str] | None = None,
        poll_s: float = 2.0,
        active_window_s: float = 600.0,
        idle_end_s: float = 180.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.engine = engine
        self.store = store
        self.projects_dir = str(projects_dir) if projects_dir else str(default_projects_dir())
        self.poll_s = poll_s
        self.active_window_s = active_window_s
        self.idle_end_s = idle_end_s
        self._clock = clock
        self._files: dict[str, _File] = {}
        self._first_poll = True
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="claude-session-watcher")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for st in self._files.values():
            if st.active:
                self._end(st, "ok")

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("claude session watcher poll failed")
            await asyncio.sleep(self.poll_s)

    def _enabled(self) -> bool:
        try:
            return bool(self.store.get_settings().get("watch_external", True))
        except Exception:
            return True

    # ----------------------------------------------------------------- poll
    async def poll_once(self) -> None:
        """One scan + read + idle-check pass (public for tests)."""
        if not self._enabled():
            return
        entries = await asyncio.to_thread(_scan, self.projects_dir)
        now = self._clock()
        first = self._first_poll
        self._first_poll = False
        # parents before children
        entries.sort(key=lambda e: (e[0] != "main", e[1]))
        seen: set[str] = set()
        for kind, path, sid, agent_id, size, mtime, ino in entries:
            seen.add(path)
            if sid in OWN_SESSION_IDS:
                continue
            st = self._files.get(path)
            if st is None:
                st = _File(kind, path, sid, agent_id, ino=ino, size=size, mtime=mtime)
                self._files[path] = st
                if first and now - mtime > self.active_window_s:
                    st.dormant = True
                    st.offset = size            # only what is appended from now on
                    continue
            elif st.ino and ino != st.ino:
                await self._rotate(st)
            elif size < st.offset:
                await self._rotate(st)
            st.size, st.mtime, st.ino = size, mtime, ino
            if size > st.offset:
                st.dormant = False
                try:
                    await self._consume(st)
                except Exception:
                    log.exception("failed to process %s", path)
        # idle / finished checks (children first)
        for st in sorted(self._files.values(), key=lambda s: s.kind != "sub"):
            if not st.active:
                continue
            if st.path not in seen:           # file deleted
                self._end(st, "ok")
                continue
            if st.offset < st.size:
                continue
            if st.kind == "sub":
                meta = await asyncio.to_thread(_read_meta, st.path[:-6] + ".meta.json")
                if meta and meta.get("finished"):
                    self._end(st, "ok")
                    continue
            last = st.mtime
            if st.kind == "main":             # a running sub-agent keeps its parent alive
                for ch in self._files.values():
                    if ch.kind == "sub" and ch.active and self._main_path(ch) == st.path:
                        last = max(last, ch.mtime)
            if now - last > self.idle_end_s:
                self._end(st, "ok")

    async def _rotate(self, st: _File) -> None:
        """File truncated or replaced: close the old run, start over."""
        if st.active:
            self._end(st, "ok")          # also moves to the next generation
        st.offset = 0
        st.run_id = st.key = None
        st.tr = StreamTranslator()
        st.title = st.cwd = None
        st.first_ts = st.last_ts = None
        st.session_sent = False
        st.dormant = False

    # ------------------------------------------------------------- consume
    async def _consume(self, st: _File) -> None:
        objs, consumed, size = await asyncio.to_thread(_read_chunk, st.path, st.offset)
        st.offset += consumed
        st.size = max(st.size, size)
        if not objs:
            return
        for obj in objs:
            t = obj.get("type")
            if t not in ("user", "assistant"):
                continue
            if st.cwd is None and isinstance(obj.get("cwd"), str):
                st.cwd = obj["cwd"]
            ts = parse_ts(obj.get("timestamp"))
            if ts is not None:
                if st.first_ts is None:
                    st.first_ts = ts
                st.last_ts = ts
            if st.title is None:
                text = user_prompt_text(obj)
                if _is_title_prompt(text):
                    st.title = make_title(text)
            if not st.active:
                if not await self._begin(st):
                    return
            self._feed(st, obj, ts)

    async def _begin(self, st: _File) -> bool:
        parent_key: str | None = None
        if st.kind == "sub":
            parent = self._files.get(self._main_path(st))
            if parent is None or parent.session_id in OWN_SESSION_IDS:
                return False
            if not parent.active:
                head = await asyncio.to_thread(_peek_head, parent.path)
                parent.title = parent.title or head["title"]
                parent.cwd = parent.cwd or head["cwd"]
                parent.first_ts = parent.first_ts or head["first_ts"]
                if not await self._begin(parent):
                    return False
            parent_key = parent.key
        if st.title is None or st.cwd is None or st.first_ts is None:
            head = await asyncio.to_thread(_peek_head, st.path)
            st.title = st.title or head["title"]
            st.cwd = st.cwd or head["cwd"]
            st.first_ts = st.first_ts or head["first_ts"]
        title = st.title
        if st.kind == "sub":
            meta = st.meta or await asyncio.to_thread(_read_meta, st.path[:-6] + ".meta.json") or {}
            st.meta = meta
            label = " — ".join(x for x in (meta.get("agentType"), meta.get("description")) if x)
            title = make_title(label) if label else title
        info: dict[str, Any] = {
            "runtime": "claude_code",
            "title": make_title(title),
            "cwd": st.cwd,
            "model": st.tr.model,
            "session_id": st.session_id,
            "started_at": st.first_ts if st.first_ts is not None else int(self._clock() * 1000),
        }
        if parent_key:
            info["parent_key"] = parent_key
        st.key = st.new_run_key()
        st.run_id = self.engine.external_begin(st.key, info)
        st.active = True
        return True

    def _main_path(self, st: _File) -> str:
        # <project>/<sid>/subagents/agent-x.jsonl -> <project>/<sid>.jsonl
        sub_dir = os.path.dirname(st.path)               # .../<sid>/subagents
        sid_dir = os.path.dirname(sub_dir)               # .../<sid>
        return sid_dir + ".jsonl"

    def _feed(self, st: _File, obj: dict[str, Any], ts: int | None) -> None:
        assert st.run_id is not None
        if obj.get("type") == "assistant" and not st.session_sent:
            model = (obj.get("message") or {}).get("model")
            if model:
                st.session_sent = True
                self.engine.external_event(st.run_id, RuntimeEvent("session", {
                    "session_id": st.session_id, "model": model,
                    "context_window": guess_context_window(model),
                }), ts)
        for ev in st.tr.feed(obj):
            self.engine.external_event(st.run_id, ev, ts)

    def _end(self, st: _File, status: str) -> None:
        if not st.active or st.run_id is None:
            return
        st.active = False
        try:
            for ev in st.tr.finish():
                self.engine.external_event(st.run_id, ev, st.last_ts)
            self.engine.external_end(st.run_id, status, st.last_ts)
        except Exception:
            log.exception("external_end failed for %s", st.key)
        finally:
            # growth after this point is a new run of the same session (key suffix #2, #3 ...)
            st.gen += 1
            st.run_id = st.key = None
            st.first_ts = None
