"""SQLite persistence for Оркестр (implements interfaces.Store).

One stdlib `sqlite3` connection in WAL mode, guarded by an RLock. The schema is
created and migrated on open (`PRAGMA user_version`). Every mutation publishes
the SSE event of docs/CONTRACT.md §5 on the bus, with the full object.

Design notes
- Agent `live` is never stored: it is computed from the `runs` (and running
  delegation `steps`) tables. When a run/step mutation changes the live block of
  its agent, an `agent` event is published, coalesced to ~4 per second per agent
  (leading edge immediately, trailing edge always delivers the final state).
  Coalescing needs a running event loop; without one (plain sync use, tests)
  events are published immediately.
- `active_runs` counts runs with status "running" (queued runs are waiting, they
  are not active).
- Runs left "running"/"queued" by a previous process are marked "error" when the
  database is opened (nothing can be running in a fresh process).
- Runs keep their `agent_id` / `agent_name` after the agent is deleted, so
  history survives; `overview` groups them by that id/name.
- Extra (non-protocol) helpers: `delete_demo_data`, `close`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from .interfaces import StoreError

log = logging.getLogger("orkestr.store")

SCHEMA_VERSION = 1

RUNTIMES = ("claude_code", "opencode", "demo")
PERMISSIONS = ("read", "edit", "full")
COLORS = ("a", "b", "c", "d", "e", "f")
RUN_STATUSES = ("queued", "running", "ok", "error", "cancelled")
RUN_ORIGINS = ("user", "delegation", "external")
STEP_KINDS = ("message", "thinking", "tool", "delegation", "system", "error")
STEP_STATUSES = ("running", "ok", "error")
SERVER_KINDS = ("lmstudio", "llamacpp", "openai_compat", "anthropic")

STEP_INPUT_MAX = 4000
STEP_OUTPUT_MAX = 20000
HARDWARE_KEEP_MS = 6 * 3600 * 1000
LATENCY_KEEP_MS = 6 * 3600 * 1000
RETENTION_ROOT_RUNS = 2000
AGENT_EVENT_MIN_INTERVAL = 0.25  # seconds -> at most 4 events/s/agent

DEFAULT_SETTINGS: dict[str, Any] = {
    "max_depth": 3,
    "max_concurrent_runs": 4,
    "delegate_wait_s": 120,
    "run_timeout_s": 1800,
    "default_cwd": "",  # filled with the process cwd on first open
    "watch_external": True,
    "demo_auto": False,
}

BUILTIN_SERVERS = (
    ("lmstudio", "LM Studio", "lmstudio", "http://127.0.0.1:1234"),
    ("llamacpp", "llama.cpp", "llamacpp", "http://127.0.0.1:8080"),
    ("anthropic", "Anthropic API", "anthropic", "https://api.anthropic.com"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, runtime TEXT NOT NULL, model TEXT,
    role TEXT NOT NULL DEFAULT '', system_prompt TEXT NOT NULL DEFAULT '',
    cwd TEXT, permissions TEXT NOT NULL DEFAULT 'edit', color TEXT,
    x REAL NOT NULL DEFAULT 0, y REAL NOT NULL DEFAULT 0,
    demo INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS agents_name_ci ON agents(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY, supervisor_id TEXT NOT NULL, subordinate_id TEXT NOT NULL,
    created_at INTEGER NOT NULL, UNIQUE (supervisor_id, subordinate_id)
);
CREATE INDEX IF NOT EXISTS edges_sub ON edges(subordinate_id);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, agent_id TEXT, agent_name TEXT NOT NULL DEFAULT '',
    runtime TEXT NOT NULL DEFAULT '', model TEXT, task TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'user',
    parent_run_id TEXT, root_run_id TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0,
    parent_step_id TEXT, session_id TEXT, cwd TEXT,
    started_at INTEGER NOT NULL, ended_at INTEGER, duration_ms INTEGER,
    result TEXT, error TEXT, activity TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0, context_used INTEGER, context_window INTEGER,
    num_turns INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
    delegations INTEGER NOT NULL DEFAULT 0, demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_agent ON runs(agent_id, started_at);
CREATE INDEX IF NOT EXISTS runs_root ON runs(root_run_id);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
    kind TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running', started_at INTEGER NOT NULL,
    ended_at INTEGER, duration_ms INTEGER, input TEXT, output TEXT,
    tool_id TEXT, child_run_id TEXT
);
CREATE INDEX IF NOT EXISTS steps_run ON steps(run_id, started_at, seq);
CREATE INDEX IF NOT EXISTS steps_deleg ON steps(kind, status);
CREATE TABLE IF NOT EXISTS servers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, base_url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, builtin INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL, status TEXT
);
CREATE TABLE IF NOT EXISTS server_samples (
    server_id TEXT NOT NULL, ts INTEGER NOT NULL, latency_ms INTEGER, up INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS server_samples_idx ON server_samples(server_id, ts);
CREATE TABLE IF NOT EXISTS hardware (ts INTEGER PRIMARY KEY, data TEXT NOT NULL);
"""

AGENT_COLS = ("id", "name", "runtime", "model", "role", "system_prompt", "cwd", "permissions",
              "color", "x", "y", "demo", "created_at", "updated_at")
RUN_COLS = ("id", "agent_id", "agent_name", "runtime", "model", "task", "status", "origin",
            "parent_run_id", "root_run_id", "depth", "parent_step_id", "session_id", "cwd",
            "started_at", "ended_at", "duration_ms", "result", "error", "activity",
            "tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens", "cost_usd",
            "context_used", "context_window", "num_turns", "tool_calls", "delegations", "demo")
STEP_COLS = ("id", "run_id", "seq", "kind", "name", "title", "status", "started_at", "ended_at",
             "duration_ms", "input", "output", "tool_id", "child_run_id")
SERVER_COLS = ("id", "name", "kind", "base_url", "enabled", "builtin", "created_at", "status")

# run fields whose change can alter the agent's `live` block
_LIVE_RUN_FIELDS = frozenset({
    "status", "activity", "tokens_in", "tokens_out", "cost_usd", "context_used",
    "context_window", "model", "ended_at", "started_at", "agent_id",
})

_INT_RUN_FIELDS = ("depth", "tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens",
                   "num_turns", "tool_calls", "delegations")


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return secrets.token_hex(6)


def _trunc(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {k: _trunc(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_trunc(v, limit) for v in value]
    return value


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _percentile(sorted_vals: list[int], p: float) -> int | None:
    if not sorted_vals:
        return None
    rank = max(1, math.ceil(p * len(sorted_vals)))
    return sorted_vals[min(rank, len(sorted_vals)) - 1]


def _avg(vals: list[int]) -> int | None:
    return int(round(sum(vals) / len(vals))) if vals else None


class SqliteStore:
    retention_root_runs = RETENTION_ROOT_RUNS

    def __init__(self, path: str, bus: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self.bus = bus
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        # agent event coalescing state
        self._live_pub: dict[str, str] = {}          # agent_id -> JSON of last published live
        self._live_last: dict[str, float] = {}       # agent_id -> monotonic time of last publish
        self._live_timer: dict[str, Any] = {}        # agent_id -> pending timer handle
        self._last_hw_prune = 0.0
        self._last_sample_prune = 0.0

        if path != ":memory:":
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        self._seed()
        self._recover_orphans()

    # ------------------------------------------------------------------ setup
    def _migrate(self) -> None:
        with self._lock:
            ver = self._db.execute("PRAGMA user_version").fetchone()[0]
            if ver > SCHEMA_VERSION:
                log.warning("database schema %s is newer than supported %s", ver, SCHEMA_VERSION)
                return
            self._db.executescript(_SCHEMA)
            # future migrations: `if ver < 2: ALTER TABLE ...`
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _seed(self) -> None:
        with self._lock:
            have = {r[0] for r in self._db.execute("SELECT key FROM settings")}
            for k, v in DEFAULT_SETTINGS.items():
                if k not in have:
                    if k == "default_cwd":
                        v = os.getcwd()
                    self._db.execute("INSERT INTO settings(key,value) VALUES (?,?)", (k, json.dumps(v)))
            ts = now_ms()
            for sid, name, kind, url in BUILTIN_SERVERS:
                exists = self._db.execute("SELECT 1 FROM servers WHERE id=?", (sid,)).fetchone()
                if not exists:
                    self._db.execute(
                        "INSERT INTO servers(id,name,kind,base_url,enabled,builtin,created_at,status)"
                        " VALUES (?,?,?,?,1,1,?,NULL)", (sid, name, kind, url, ts))

    def _recover_orphans(self) -> None:
        ts = now_ms()
        with self._lock:
            cur = self._db.execute(
                "UPDATE runs SET status='error', ended_at=?, duration_ms=MAX(0, ?-started_at),"
                " activity=NULL, error=COALESCE(error, 'Сервер был перезапущен, запуск прерван')"
                " WHERE status IN ('running','queued')", (ts, ts))
            if cur.rowcount:
                log.info("marked %d orphaned runs as error", cur.rowcount)
            self._db.execute(
                "UPDATE steps SET status='error', ended_at=?, duration_ms=MAX(0, ?-started_at)"
                " WHERE status='running'", (ts, ts))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for h in self._live_timer.values():
                try:
                    h.cancel()
                except Exception:
                    pass
            self._live_timer.clear()
            try:
                self._db.close()
            except Exception:
                log.exception("closing database failed")

    def _pub(self, type_: str, data: dict[str, Any]) -> None:
        try:
            self.bus.publish(type_, data)
        except Exception:  # bus must not raise, but never let it break a mutation
            log.exception("publish %s failed", type_)

    # --------------------------------------------------------------- settings
    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            out = dict(DEFAULT_SETTINGS)
            for r in self._db.execute("SELECT key, value FROM settings"):
                if r["key"] in out:
                    try:
                        out[r["key"]] = json.loads(r["value"])
                    except ValueError:
                        pass
            if not out.get("default_cwd"):
                out["default_cwd"] = os.getcwd()
            return out

    @staticmethod
    def _validate_settings(patch: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        ranges = {"max_depth": (0, 20), "max_concurrent_runs": (1, 64),
                  "delegate_wait_s": (1, 3600), "run_timeout_s": (10, 86400)}
        for key, val in patch.items():
            if key not in DEFAULT_SETTINGS:
                continue  # unknown keys are ignored
            if key in ranges:
                if isinstance(val, float) and val.is_integer():
                    val = int(val)
                lo, hi = ranges[key]
                if not _is_int(val) or not lo <= val <= hi:
                    raise StoreError("validation", f"Параметр «{key}» должен быть целым числом от {lo} до {hi}")
            elif key in ("watch_external", "demo_auto"):
                if not isinstance(val, bool):
                    raise StoreError("validation", f"Параметр «{key}» должен быть true или false")
            elif key == "default_cwd":
                if val is None or val == "":
                    val = os.getcwd()
                if not isinstance(val, str):
                    raise StoreError("validation", "Параметр «default_cwd» должен быть строкой")
            clean[key] = val
        return clean

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        clean = self._validate_settings(patch or {})
        with self._lock:
            for k, v in clean.items():
                self._db.execute("INSERT INTO settings(key,value) VALUES (?,?)"
                                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))
            out = self.get_settings()
            self._pub("settings", out)
            return out

    # ----------------------------------------------------------------- agents
    @staticmethod
    def _validate_agent_fields(d: dict[str, Any], *, partial: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "name" in d or not partial:
            name = d.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
                raise StoreError("validation", "Имя агента должно содержать от 1 до 40 символов")
            out["name"] = name.strip()
        if "runtime" in d or not partial:
            if d.get("runtime") not in RUNTIMES:
                raise StoreError("validation", "Неизвестная среда выполнения агента")
            out["runtime"] = d["runtime"]
        if "model" in d:
            m = d["model"]
            if m is not None and not isinstance(m, str):
                raise StoreError("validation", "Модель должна быть строкой или null")
            out["model"] = m or None
        for key, label in (("role", "Роль"), ("system_prompt", "Системная инструкция")):
            if key in d:
                if d[key] is None:
                    d[key] = ""
                if not isinstance(d[key], str):
                    raise StoreError("validation", f"{label} должна быть строкой")
                out[key] = d[key]
        if "cwd" in d:
            c = d["cwd"]
            if c is not None and not isinstance(c, str):
                raise StoreError("validation", "Папка проекта должна быть строкой или null")
            out["cwd"] = c or None
        if "permissions" in d:
            if d["permissions"] not in PERMISSIONS:
                raise StoreError("validation", "Права доступа: read, edit или full")
            out["permissions"] = d["permissions"]
        if "color" in d:
            if d["color"] is not None and d["color"] not in COLORS:
                raise StoreError("validation", "Цвет: null или буква от a до f")
            out["color"] = d["color"]
        for key in ("x", "y"):
            if key in d:
                if not _is_num(d[key]):
                    raise StoreError("validation", f"Координата {key} должна быть числом")
                out[key] = float(d[key])
        if "demo" in d:
            if not isinstance(d["demo"], bool):
                raise StoreError("validation", "Поле demo должно быть true или false")
            out["demo"] = d["demo"]
        return out

    def _name_taken(self, name: str, except_id: str | None = None) -> bool:
        row = self._db.execute("SELECT id FROM agents WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        return bool(row and row["id"] != except_id)

    def _agent_row(self, row: sqlite3.Row, live: dict[str, Any]) -> dict[str, Any]:
        a = {k: row[k] for k in AGENT_COLS}
        a["demo"] = bool(a["demo"])
        a["x"] = float(a["x"])
        a["y"] = float(a["y"])
        a["live"] = live
        return a

    def _idle_live(self) -> dict[str, Any]:
        return {"status": "idle", "active_runs": 0, "current_run_id": None, "activity": None,
                "context_used": None, "context_window": None, "model": None,
                "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "runs_total": 0,
                "last_run_at": None, "last_status": None}

    def _live_map(self, agent_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
        """Compute `live` for the given agents (all agents when None)."""
        flt = ""
        params: tuple = ()
        if agent_ids is not None:
            if not agent_ids:
                return {}
            flt = " AND agent_id IN (SELECT value FROM json_each(?))"
            params = (json.dumps(agent_ids),)
        live: dict[str, dict[str, Any]] = {}

        def get(aid: str) -> dict[str, Any]:
            if aid not in live:
                live[aid] = self._idle_live()
            return live[aid]

        q = self._db.execute(
            "SELECT agent_id, COUNT(*) n, COALESCE(SUM(tokens_in),0) ti, COALESCE(SUM(tokens_out),0) tout,"
            " COALESCE(SUM(cost_usd),0) cost, MAX(started_at) last_at FROM runs"
            " WHERE agent_id IS NOT NULL" + flt + " GROUP BY agent_id", params)
        for r in q:
            lv = get(r["agent_id"])
            lv.update(runs_total=r["n"], tokens_in=r["ti"], tokens_out=r["tout"],
                      cost_usd=float(r["cost"]), last_run_at=r["last_at"])

        # last finished run per agent
        q = self._db.execute(
            "SELECT agent_id, status, model, context_used, context_window FROM ("
            " SELECT agent_id, status, model, context_used, context_window,"
            "  ROW_NUMBER() OVER (PARTITION BY agent_id ORDER BY COALESCE(ended_at, started_at) DESC, rowid DESC) rn"
            "  FROM runs WHERE status IN ('ok','error','cancelled') AND agent_id IS NOT NULL" + flt +
            ") WHERE rn=1", params)
        for r in q:
            lv = get(r["agent_id"])
            lv.update(last_status=r["status"], model=r["model"],
                      context_used=r["context_used"], context_window=r["context_window"])

        # running runs: newest first
        seen: set[str] = set()
        q = self._db.execute(
            "SELECT id, agent_id, activity, model, context_used, context_window FROM runs"
            " WHERE status='running' AND agent_id IS NOT NULL" + flt +
            " ORDER BY started_at DESC, rowid DESC", params)
        for r in q:
            lv = get(r["agent_id"])
            lv["active_runs"] += 1
            if r["agent_id"] not in seen:
                seen.add(r["agent_id"])
                lv.update(current_run_id=r["id"], activity=r["activity"],
                          context_used=r["context_used"], context_window=r["context_window"],
                          model=r["model"], status="working")

        q = self._db.execute(
            "SELECT DISTINCT r.agent_id FROM runs r JOIN steps s ON s.run_id=r.id"
            " WHERE r.status='running' AND s.kind='delegation' AND s.status='running'"
            " AND r.agent_id IS NOT NULL" + flt.replace("agent_id", "r.agent_id"), params)
        for r in q:
            get(r["agent_id"])["status"] = "delegating"

        for lv in live.values():
            if lv["status"] == "idle" and lv["last_status"] == "error":
                lv["status"] = "error"
        return live

    def _agents_where(self, where: str = "", params: tuple = (), order: str = "created_at, rowid") -> list[dict[str, Any]]:
        rows = self._db.execute(f"SELECT {','.join(AGENT_COLS)} FROM agents {where} ORDER BY {order}", params).fetchall()
        lives = self._live_map([r["id"] for r in rows])
        return [self._agent_row(r, lives.get(r["id"]) or self._idle_live()) for r in rows]

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._agents_where()

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            res = self._agents_where("WHERE id=?", (agent_id,))
            return res[0] if res else None

    def get_agent_by_name(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            res = self._agents_where("WHERE name=?", (name,))
            if not res:
                res = self._agents_where("WHERE name=? COLLATE NOCASE", (name,))
            return res[0] if res else None

    def create_agent(self, data: dict[str, Any]) -> dict[str, Any]:
        f = self._validate_agent_fields(dict(data), partial=False)
        with self._lock:
            if self._name_taken(f["name"]):
                raise StoreError("name_taken", f"Агент с именем «{f['name']}» уже существует")
            ts = now_ms()
            row = {
                "id": new_id(), "name": f["name"], "runtime": f["runtime"], "model": f.get("model"),
                "role": f.get("role", ""), "system_prompt": f.get("system_prompt", ""),
                "cwd": f.get("cwd"), "permissions": f.get("permissions", "edit"),
                "color": f.get("color"), "x": f.get("x", 0.0), "y": f.get("y", 0.0),
                "demo": int(f.get("demo", f["runtime"] == "demo")), "created_at": ts, "updated_at": ts,
            }
            self._db.execute(
                f"INSERT INTO agents({','.join(AGENT_COLS)}) VALUES ({','.join('?' * len(AGENT_COLS))})",
                tuple(row[c] for c in AGENT_COLS))
            agent = self.get_agent(row["id"])
            assert agent is not None
            self._publish_agent(agent)
            return agent

    def update_agent(self, agent_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        f = self._validate_agent_fields(dict(patch), partial=True)
        with self._lock:
            if not self._db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                return None
            if "name" in f and self._name_taken(f["name"], agent_id):
                raise StoreError("name_taken", f"Агент с именем «{f['name']}» уже существует")
            if f:
                f["updated_at"] = now_ms()
                if "demo" in f:
                    f["demo"] = int(f["demo"])
                sets = ", ".join(f"{k}=?" for k in f)
                self._db.execute(f"UPDATE agents SET {sets} WHERE id=?", (*f.values(), agent_id))
                if "name" in f:
                    self._db.execute("UPDATE runs SET agent_name=? WHERE agent_id=? AND status IN ('queued','running')",
                                     (f["name"], agent_id))
            agent = self.get_agent(agent_id)
            assert agent is not None
            if f:
                self._publish_agent(agent)
            return agent

    def delete_agent(self, agent_id: str) -> bool:
        with self._lock:
            if not self._db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                return False
            edge_ids = [r[0] for r in self._db.execute(
                "SELECT id FROM edges WHERE supervisor_id=? OR subordinate_id=?", (agent_id, agent_id))]
            self._db.execute("DELETE FROM edges WHERE supervisor_id=? OR subordinate_id=?", (agent_id, agent_id))
            self._db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
            self._forget_agent(agent_id)
            for eid in edge_ids:
                self._pub("edge.deleted", {"id": eid})
            self._pub("agent.deleted", {"id": agent_id})
            return True

    def _forget_agent(self, agent_id: str) -> None:
        h = self._live_timer.pop(agent_id, None)
        if h is not None:
            try:
                h.cancel()
            except Exception:
                pass
        self._live_pub.pop(agent_id, None)
        self._live_last.pop(agent_id, None)

    # --- agent event publishing / coalescing
    def _publish_agent(self, agent: dict[str, Any]) -> None:
        aid = agent["id"]
        self._live_pub[aid] = json.dumps(agent["live"], sort_keys=True)
        self._live_last[aid] = self._clock()
        h = self._live_timer.pop(aid, None)
        if h is not None:
            try:
                h.cancel()
            except Exception:
                pass
        self._pub("agent", agent)

    def _touch_agent(self, agent_id: str | None) -> None:
        """A run/step of this agent changed: publish the agent if its live changed."""
        if not agent_id:
            return
        live = self._live_map([agent_id]).get(agent_id)
        if live is None:
            live = self._idle_live()
        if not self._db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
            return
        sig = json.dumps(live, sort_keys=True)
        pending = agent_id in self._live_timer
        if sig == self._live_pub.get(agent_id) and not pending:
            return
        wait = self._live_last.get(agent_id, 0.0) + AGENT_EVENT_MIN_INTERVAL - self._clock()
        if wait <= 0:
            self._flush_agent(agent_id)
            return
        if pending:
            return  # the trailing publish recomputes the final state
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_agent(agent_id)  # no loop to schedule on
            return
        self._live_timer[agent_id] = loop.call_later(wait, self._timer_fired, agent_id)

    def _timer_fired(self, agent_id: str) -> None:
        with self._lock:
            self._live_timer.pop(agent_id, None)
            if self._closed:
                return
            self._flush_agent(agent_id)

    def _flush_agent(self, agent_id: str) -> None:
        agent = self.get_agent(agent_id)
        if agent is not None:
            self._publish_agent(agent)

    # ------------------------------------------------------------------ edges
    def _edge(self, r: sqlite3.Row) -> dict[str, Any]:
        return {"id": r["id"], "supervisor_id": r["supervisor_id"],
                "subordinate_id": r["subordinate_id"], "created_at": r["created_at"]}

    def list_edges(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._edge(r) for r in self._db.execute(
                "SELECT * FROM edges ORDER BY created_at, rowid")]

    def create_edge(self, supervisor_id: str, subordinate_id: str) -> dict[str, Any]:
        with self._lock:
            for aid in (supervisor_id, subordinate_id):
                if not isinstance(aid, str) or not self._db.execute(
                        "SELECT 1 FROM agents WHERE id=?", (aid,)).fetchone():
                    raise StoreError("unknown_agent", "Агент не найден")
            if supervisor_id == subordinate_id:
                raise StoreError("self_edge", "Агент не может быть руководителем самого себя")
            if self._db.execute("SELECT 1 FROM edges WHERE supervisor_id=? AND subordinate_id=?",
                                (supervisor_id, subordinate_id)).fetchone():
                raise StoreError("duplicate_edge", "Такая связь уже есть")
            # a cycle appears iff the supervisor is already reachable from the subordinate
            children: dict[str, list[str]] = {}
            for r in self._db.execute("SELECT supervisor_id, subordinate_id FROM edges"):
                children.setdefault(r[0], []).append(r[1])
            stack, seen = [subordinate_id], {subordinate_id}
            while stack:
                cur = stack.pop()
                if cur == supervisor_id:
                    raise StoreError("cycle", "Связь создаёт цикл в иерархии")
                for nxt in children.get(cur, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            edge = {"id": new_id(), "supervisor_id": supervisor_id,
                    "subordinate_id": subordinate_id, "created_at": now_ms()}
            self._db.execute("INSERT INTO edges(id,supervisor_id,subordinate_id,created_at) VALUES (?,?,?,?)",
                             (edge["id"], supervisor_id, subordinate_id, edge["created_at"]))
            self._pub("edge", edge)
            return edge

    def delete_edge(self, edge_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM edges WHERE id=?", (edge_id,))
            if cur.rowcount:
                self._pub("edge.deleted", {"id": edge_id})
                return True
            return False

    def subordinates_of(self, agent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._agents_where(
                "WHERE id IN (SELECT subordinate_id FROM edges WHERE supervisor_id=?)", (agent_id,))

    def supervisors_of(self, agent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._agents_where(
                "WHERE id IN (SELECT supervisor_id FROM edges WHERE subordinate_id=?)", (agent_id,))

    # ------------------------------------------------------------------- runs
    @staticmethod
    def _run(r: sqlite3.Row) -> dict[str, Any]:
        d = {k: r[k] for k in RUN_COLS}
        d["demo"] = bool(d["demo"])
        d["cost_usd"] = float(d["cost_usd"] or 0.0)
        return d

    @staticmethod
    def _check_run_fields(d: dict[str, Any]) -> None:
        if "status" in d and d["status"] not in RUN_STATUSES:
            raise StoreError("validation", "Неизвестный статус запуска")
        if "origin" in d and d["origin"] not in RUN_ORIGINS:
            raise StoreError("validation", "Неизвестное происхождение запуска")
        for k in _INT_RUN_FIELDS:
            if k in d and not _is_int(d[k]):
                raise StoreError("validation", f"Поле {k} должно быть целым числом")
        if "cost_usd" in d and not _is_num(d["cost_usd"]):
            raise StoreError("validation", "Поле cost_usd должно быть числом")

    def create_run(self, data: dict[str, Any]) -> dict[str, Any]:
        d = {k: v for k, v in data.items() if k in RUN_COLS}
        self._check_run_fields(d)
        with self._lock:
            rid = d.get("id") or new_id()
            agent = None
            if d.get("agent_id"):
                agent = self._db.execute("SELECT name, runtime, model, demo FROM agents WHERE id=?",
                                         (d["agent_id"],)).fetchone()
            parent = None
            if d.get("parent_run_id"):
                parent = self._db.execute("SELECT root_run_id, depth FROM runs WHERE id=?",
                                          (d["parent_run_id"],)).fetchone()
                if parent is None:
                    raise StoreError("not_found", "Родительский запуск не найден")
            row: dict[str, Any] = {
                "id": rid, "agent_id": d.get("agent_id"),
                "agent_name": d.get("agent_name") or (agent["name"] if agent else ""),
                "runtime": d.get("runtime") or (agent["runtime"] if agent else ""),
                "model": d.get("model") if "model" in d else (agent["model"] if agent else None),
                "task": d.get("task", ""), "status": d.get("status", "queued"),
                "origin": d.get("origin", "delegation" if d.get("parent_run_id") else "user"),
                "parent_run_id": d.get("parent_run_id"),
                "root_run_id": d.get("root_run_id") or (parent["root_run_id"] if parent else rid),
                "depth": d["depth"] if "depth" in d else (parent["depth"] + 1 if parent else 0),
                "parent_step_id": d.get("parent_step_id"), "session_id": d.get("session_id"),
                "cwd": d.get("cwd"), "started_at": d.get("started_at") or now_ms(),
                "ended_at": d.get("ended_at"), "duration_ms": d.get("duration_ms"),
                "result": d.get("result"), "error": d.get("error"), "activity": d.get("activity"),
                "tokens_in": 0, "tokens_out": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                "cost_usd": 0.0, "context_used": d.get("context_used"),
                "context_window": d.get("context_window"), "num_turns": 0, "tool_calls": 0,
                "delegations": 0,
                "demo": int(bool(d["demo"])) if "demo" in d else (int(agent["demo"]) if agent else 0),
            }
            for k in ("tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens",
                      "cost_usd", "num_turns", "tool_calls", "delegations"):
                if k in d:
                    row[k] = d[k]
            if row["ended_at"] is not None and row["duration_ms"] is None:
                row["duration_ms"] = max(0, row["ended_at"] - row["started_at"])
            try:
                self._db.execute(
                    f"INSERT INTO runs({','.join(RUN_COLS)}) VALUES ({','.join('?' * len(RUN_COLS))})",
                    tuple(row[c] for c in RUN_COLS))
            except sqlite3.IntegrityError:
                raise StoreError("validation", "Запуск с таким id уже существует") from None
            run = self.get_run(rid)
            assert run is not None
            self._pub("run", run)
            self._touch_agent(run["agent_id"])
            if run["parent_run_id"] is None:
                self._enforce_retention()
            return run

    def update_run(self, run_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        d = {k: v for k, v in patch.items() if k in RUN_COLS and k != "id"}
        self._check_run_fields(d)
        with self._lock:
            cur = self._db.execute("SELECT started_at, agent_id FROM runs WHERE id=?", (run_id,)).fetchone()
            if cur is None:
                return None
            if d.get("ended_at") is not None:
                started = d.get("started_at") or cur["started_at"]
                d["duration_ms"] = max(0, d["ended_at"] - started)
            if "demo" in d:
                d["demo"] = int(bool(d["demo"]))
            if d:
                sets = ", ".join(f"{k}=?" for k in d)
                self._db.execute(f"UPDATE runs SET {sets} WHERE id=?", (*d.values(), run_id))
            run = self.get_run(run_id)
            assert run is not None
            self._pub("run", run)
            if _LIVE_RUN_FIELDS & d.keys():
                self._touch_agent(run["agent_id"])
                if cur["agent_id"] != run["agent_id"]:
                    self._touch_agent(cur["agent_id"])
            return run

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute(f"SELECT {','.join(RUN_COLS)} FROM runs WHERE id=?", (run_id,)).fetchone()
            return self._run(r) if r else None

    def list_runs(self, *, status: str | None = None, agent_id: str | None = None,
                  origin: str | None = None, root_only: bool = False, limit: int = 50,
                  before: int | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        if status:
            sts = [s.strip() for s in status.split(",") if s.strip()]
            where.append("status IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(sts))
        if agent_id:
            where.append("agent_id=?")
            params.append(agent_id)
        if origin:
            where.append("origin=?")
            params.append(origin)
        if root_only:
            where.append("parent_run_id IS NULL")
        if before is not None:
            where.append("started_at < ?")
            params.append(before)
        sql = f"SELECT {','.join(RUN_COLS)} FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            return [self._run(r) for r in self._db.execute(sql, params)]

    def run_tree(self, root_run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {','.join(RUN_COLS)} FROM runs WHERE root_run_id=? ORDER BY depth, started_at, rowid",
                (root_run_id,)).fetchall()
            return [self._run(r) for r in rows]

    def _enforce_retention(self) -> None:
        keep = self.retention_root_runs
        n = self._db.execute("SELECT COUNT(*) FROM runs WHERE parent_run_id IS NULL").fetchone()[0]
        if n <= keep:
            return
        old = [r[0] for r in self._db.execute(
            "SELECT id FROM runs WHERE parent_run_id IS NULL AND status NOT IN ('running','queued')"
            " ORDER BY started_at DESC, rowid DESC LIMIT -1 OFFSET ?", (keep,))]
        # roots that are still active count towards `keep` but are never deleted
        if old:
            self._delete_trees(old)

    def _delete_trees(self, root_ids: list[str]) -> None:
        arg = json.dumps(root_ids)
        self._db.execute("DELETE FROM steps WHERE run_id IN (SELECT id FROM runs WHERE root_run_id IN"
                         " (SELECT value FROM json_each(?)))", (arg,))
        self._db.execute("DELETE FROM runs WHERE root_run_id IN (SELECT value FROM json_each(?))", (arg,))

    # ------------------------------------------------------------------ steps
    @staticmethod
    def _step(r: sqlite3.Row) -> dict[str, Any]:
        d = {k: r[k] for k in STEP_COLS}
        d["input"] = json.loads(d["input"]) if d["input"] is not None else None
        return d

    def create_step(self, data: dict[str, Any]) -> dict[str, Any]:
        d = {k: v for k, v in data.items() if k in STEP_COLS}
        if not d.get("run_id"):
            raise StoreError("validation", "У шага должен быть run_id")
        if d.get("kind") not in STEP_KINDS:
            raise StoreError("validation", "Неизвестный тип шага")
        if d.get("status", "running") not in STEP_STATUSES:
            raise StoreError("validation", "Неизвестный статус шага")
        with self._lock:
            run = self._db.execute("SELECT agent_id FROM runs WHERE id=?", (d["run_id"],)).fetchone()
            if run is None:
                raise StoreError("not_found", "Запуск не найден")
            seq = d.get("seq")
            if seq is None:
                seq = self._db.execute("SELECT COALESCE(MAX(seq), -1)+1 FROM steps WHERE run_id=?",
                                       (d["run_id"],)).fetchone()[0]
            started = d.get("started_at") or now_ms()
            ended = d.get("ended_at")
            name = d.get("name") or ""
            inp = d.get("input")
            out = d.get("output")
            row = {
                "id": d.get("id") or new_id(), "run_id": d["run_id"], "seq": seq, "kind": d["kind"],
                "name": name, "title": d.get("title") or name, "status": d.get("status", "running"),
                "started_at": started, "ended_at": ended,
                "duration_ms": max(0, ended - started) if ended is not None else d.get("duration_ms"),
                "input": json.dumps(_trunc(inp, STEP_INPUT_MAX), ensure_ascii=False) if inp is not None else None,
                "output": _trunc(out, STEP_OUTPUT_MAX) if isinstance(out, str) else out,
                "tool_id": d.get("tool_id"), "child_run_id": d.get("child_run_id"),
            }
            try:
                self._db.execute(
                    f"INSERT INTO steps({','.join(STEP_COLS)}) VALUES ({','.join('?' * len(STEP_COLS))})",
                    tuple(row[c] for c in STEP_COLS))
            except sqlite3.IntegrityError:
                raise StoreError("validation", "Шаг с таким id уже существует") from None
            step = self.get_step(row["id"])
            assert step is not None
            self._pub("step", step)
            if step["kind"] == "delegation":
                self._touch_agent(run["agent_id"])
            return step

    def update_step(self, step_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        d = {k: v for k, v in patch.items() if k in STEP_COLS and k not in ("id", "run_id")}
        if "kind" in d and d["kind"] not in STEP_KINDS:
            raise StoreError("validation", "Неизвестный тип шага")
        if "status" in d and d["status"] not in STEP_STATUSES:
            raise StoreError("validation", "Неизвестный статус шага")
        with self._lock:
            cur = self._db.execute(
                "SELECT s.started_at, s.kind, r.agent_id FROM steps s JOIN runs r ON r.id=s.run_id WHERE s.id=?",
                (step_id,)).fetchone()
            if cur is None:
                return None
            if d.get("ended_at") is not None:
                d["duration_ms"] = max(0, d["ended_at"] - (d.get("started_at") or cur["started_at"]))
            if "input" in d:
                d["input"] = (json.dumps(_trunc(d["input"], STEP_INPUT_MAX), ensure_ascii=False)
                              if d["input"] is not None else None)
            if isinstance(d.get("output"), str):
                d["output"] = _trunc(d["output"], STEP_OUTPUT_MAX)
            if d:
                sets = ", ".join(f"{k}=?" for k in d)
                self._db.execute(f"UPDATE steps SET {sets} WHERE id=?", (*d.values(), step_id))
            step = self.get_step(step_id)
            assert step is not None
            self._pub("step", step)
            if "delegation" in (cur["kind"], step["kind"]):
                self._touch_agent(cur["agent_id"])
            return step

    def get_step(self, step_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute(f"SELECT {','.join(STEP_COLS)} FROM steps WHERE id=?", (step_id,)).fetchone()
            return self._step(r) if r else None

    def list_steps(self, run_ids: list[str]) -> list[dict[str, Any]]:
        if not run_ids:
            return []
        with self._lock:
            rows = self._db.execute(
                f"SELECT {','.join(STEP_COLS)} FROM steps WHERE run_id IN (SELECT value FROM json_each(?))"
                " ORDER BY started_at, seq, rowid", (json.dumps(list(run_ids)),)).fetchall()
            return [self._step(r) for r in rows]

    # ---------------------------------------------------------------- servers
    @staticmethod
    def _server(r: sqlite3.Row) -> dict[str, Any]:
        d = {k: r[k] for k in SERVER_COLS}
        d["enabled"] = bool(d["enabled"])
        d["builtin"] = bool(d["builtin"])
        d["status"] = json.loads(d["status"]) if d["status"] else None
        d["location"] = "cloud" if d["kind"] == "anthropic" else "local"
        return d

    @staticmethod
    def _validate_server_fields(d: dict[str, Any], *, partial: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "name" in d or not partial:
            n = d.get("name")
            if not isinstance(n, str) or not 1 <= len(n.strip()) <= 60:
                raise StoreError("validation", "Название сервера должно содержать от 1 до 60 символов")
            out["name"] = n.strip()
        if "kind" in d or not partial:
            if d.get("kind") not in SERVER_KINDS:
                raise StoreError("validation", "Неизвестный тип сервера")
            out["kind"] = d["kind"]
        if "base_url" in d or not partial:
            u = d.get("base_url")
            if not isinstance(u, str):
                raise StoreError("validation", "Адрес сервера обязателен")
            u = u.strip().rstrip("/")
            try:
                parts = urlsplit(u)
                ok = parts.scheme in ("http", "https") and bool(parts.hostname)
                parts.port  # noqa: B018 - raises ValueError for a bad port
            except ValueError:
                ok = False
            if not ok:
                raise StoreError("validation", "Адрес сервера должен начинаться с http:// или https://")
            out["base_url"] = u
        if "enabled" in d:
            if not isinstance(d["enabled"], bool):
                raise StoreError("validation", "Поле enabled должно быть true или false")
            out["enabled"] = d["enabled"]
        return out

    def list_servers(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._server(r) for r in self._db.execute(
                f"SELECT {','.join(SERVER_COLS)} FROM servers ORDER BY builtin DESC, created_at, rowid")]

    def get_server(self, server_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute(f"SELECT {','.join(SERVER_COLS)} FROM servers WHERE id=?", (server_id,)).fetchone()
            return self._server(r) if r else None

    def create_server(self, data: dict[str, Any]) -> dict[str, Any]:
        f = self._validate_server_fields(dict(data), partial=False)
        with self._lock:
            sid = new_id()
            self._db.execute(
                "INSERT INTO servers(id,name,kind,base_url,enabled,builtin,created_at,status) VALUES (?,?,?,?,?,0,?,NULL)",
                (sid, f["name"], f["kind"], f["base_url"], int(f.get("enabled", True)), now_ms()))
            server = self.get_server(sid)
            assert server is not None
            self._pub("server", server)
            return server

    def update_server(self, server_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        patch = {k: v for k, v in patch.items() if k in ("name", "base_url", "enabled")}
        f = self._validate_server_fields(patch, partial=True)
        with self._lock:
            if self.get_server(server_id) is None:
                return None
            if f:
                if "enabled" in f:
                    f["enabled"] = int(f["enabled"])
                if "base_url" in f:
                    f["status"] = None  # old status belongs to the old address
                sets = ", ".join(f"{k}=?" for k in f)
                self._db.execute(f"UPDATE servers SET {sets} WHERE id=?", (*f.values(), server_id))
            server = self.get_server(server_id)
            assert server is not None
            if f:
                self._pub("server", server)
            return server

    def delete_server(self, server_id: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT builtin FROM servers WHERE id=?", (server_id,)).fetchone()
            if row is None:
                return False
            if row["builtin"]:
                raise StoreError("builtin", "Встроенный сервер нельзя удалить, его можно отключить")
            self._db.execute("DELETE FROM servers WHERE id=?", (server_id,))
            self._db.execute("DELETE FROM server_samples WHERE server_id=?", (server_id,))
            self._pub("server.deleted", {"id": server_id})
            return True

    def save_server_status(self, server_id: str, status: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            if self.get_server(server_id) is None:
                return None
            status = dict(status)
            status.setdefault("checked_at", now_ms())
            self._db.execute("UPDATE servers SET status=? WHERE id=?",
                             (json.dumps(status, ensure_ascii=False), server_id))
            state = status.get("state")
            if state in ("up", "down"):
                self._db.execute("INSERT INTO server_samples(server_id,ts,latency_ms,up) VALUES (?,?,?,?)",
                                 (server_id, status["checked_at"], status.get("latency_ms"), int(state == "up")))
                t = self._clock()
                if t - self._last_sample_prune > 60:
                    self._last_sample_prune = t
                    self._db.execute("DELETE FROM server_samples WHERE ts < ?", (now_ms() - LATENCY_KEEP_MS,))
            server = self.get_server(server_id)
            assert server is not None
            self._pub("server", server)
            return server

    def list_server_latency(self, server_id: str, since_ms: int) -> list[dict[str, Any]]:
        """Latency history of one server (extra, not part of the Store protocol)."""
        with self._lock:
            return [{"ts": r["ts"], "latency_ms": r["latency_ms"], "up": bool(r["up"])}
                    for r in self._db.execute(
                        "SELECT ts, latency_ms, up FROM server_samples WHERE server_id=? AND ts>=? ORDER BY ts",
                        (server_id, since_ms))]

    # --------------------------------------------------------------- hardware
    def add_hardware_sample(self, sample: dict[str, Any]) -> None:
        ts = int(sample.get("ts") or now_ms())
        sample = {**sample, "ts": ts}
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO hardware(ts,data) VALUES (?,?)",
                             (ts, json.dumps(sample, ensure_ascii=False)))
            t = self._clock()
            if t - self._last_hw_prune > 60:
                self._last_hw_prune = t
                self._db.execute("DELETE FROM hardware WHERE ts < ?", (max(ts, now_ms()) - HARDWARE_KEEP_MS,))
            self._pub("hardware", sample)

    def list_hardware_samples(self, since_ms: int, max_points: int = 300) -> list[dict[str, Any]]:
        max_points = max(2, int(max_points))
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM hardware WHERE ts>=?", (since_ms,)).fetchone()[0]
            if n == 0:
                return []
            if n <= max_points:
                rows = self._db.execute("SELECT data FROM hardware WHERE ts>=? ORDER BY ts", (since_ms,)).fetchall()
                return [json.loads(r[0]) for r in rows]
            k = math.ceil(n / (max_points - 1))  # leaves room for the final sample
            rows = self._db.execute(
                "SELECT data FROM (SELECT data, ts, ROW_NUMBER() OVER (ORDER BY ts) rn FROM hardware WHERE ts>=?)"
                " WHERE (rn-1) % ? = 0 ORDER BY ts", (since_ms, k)).fetchall()
            out = [json.loads(r[0]) for r in rows]
            last = json.loads(self._db.execute("SELECT data FROM hardware ORDER BY ts DESC LIMIT 1").fetchone()[0])
            if out[-1]["ts"] != last["ts"]:
                out.append(last)
            return out[:max_points - 1] + out[-1:] if len(out) > max_points else out

    # --------------------------------------------------------------- overview
    def overview(self, window_s: int = 3600, buckets: int = 60) -> dict[str, Any]:
        window_s = max(1, int(window_s))
        buckets = max(1, min(int(buckets), 1000))
        now = now_ms()
        wms = window_s * 1000
        start = now - wms
        prev_start = start - wms
        width = wms / buckets
        with self._lock:
            rows = self._db.execute(
                "SELECT agent_id, agent_name, runtime, model, status, started_at, ended_at, duration_ms,"
                " tokens_in, tokens_out, cost_usd, tool_calls, delegations FROM runs"
                " WHERE started_at <= ? AND (started_at >= ? OR ended_at IS NULL OR ended_at >= ?)",
                (now, prev_start, start)).fetchall()
            active_now = self._db.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0]

        cur = [r for r in rows if r["started_at"] >= start]
        prev = [r for r in rows if prev_start <= r["started_at"] < start]

        def durations(rs: list[sqlite3.Row]) -> list[int]:
            return [r["duration_ms"] for r in rs if r["duration_ms"] is not None]

        def agg(rs: list[sqlite3.Row]) -> dict[str, Any]:
            d = durations(rs)
            return {"runs": len(rs), "errors": sum(1 for r in rs if r["status"] == "error"),
                    "tokens_in": sum(r["tokens_in"] for r in rs),
                    "tokens_out": sum(r["tokens_out"] for r in rs),
                    "cost_usd": float(sum(r["cost_usd"] for r in rs)), "avg_run_ms": _avg(d)}

        c = agg(cur)
        p = agg(prev)
        dur_sorted = sorted(durations(cur))
        kpis = {
            "active_runs": active_now, "runs": c["runs"],
            "runs_ok": sum(1 for r in cur if r["status"] == "ok"), "runs_error": c["errors"],
            "delegations": sum(r["delegations"] for r in cur),
            "tool_calls": sum(r["tool_calls"] for r in cur),
            "tokens_in": c["tokens_in"], "tokens_out": c["tokens_out"], "cost_usd": c["cost_usd"],
            "avg_run_ms": c["avg_run_ms"], "p95_run_ms": _percentile(dur_sorted, 0.95),
            "prev": {"runs": p["runs"], "runs_error": p["errors"], "tokens_in": p["tokens_in"],
                     "tokens_out": p["tokens_out"], "cost_usd": p["cost_usd"], "avg_run_ms": p["avg_run_ms"]},
        }

        t = [int(start + i * width) for i in range(buckets)]
        ends = [int(start + (i + 1) * width) for i in range(buckets)]
        ends[-1] = now
        s_runs = [0] * buckets
        s_err = [0] * buckets
        s_in = [0] * buckets
        s_out = [0] * buckets
        s_cost = [0.0] * buckets
        s_act = [0] * buckets
        for r in rows:
            if r["started_at"] >= start:
                i = min(buckets - 1, int((r["started_at"] - start) / width))
                s_runs[i] += 1
                s_err[i] += r["status"] == "error"
                s_in[i] += r["tokens_in"]
                s_out[i] += r["tokens_out"]
                s_cost[i] += r["cost_usd"]
            if r["status"] == "queued":
                continue
            end = r["ended_at"]
            for i in range(buckets):
                if r["started_at"] <= ends[i] and (end is None or end > ends[i]):
                    s_act[i] += 1
        series = {"t": t, "runs": s_runs, "errors": s_err, "tokens_in": s_in, "tokens_out": s_out,
                  "cost_usd": s_cost, "active": s_act}

        def group(keyf: Callable[[sqlite3.Row], Any]) -> dict[Any, list[sqlite3.Row]]:
            g: dict[Any, list[sqlite3.Row]] = {}
            for r in cur:
                g.setdefault(keyf(r), []).append(r)
            return g

        def entry(rs: list[sqlite3.Row], with_avg: bool = True) -> dict[str, Any]:
            a = agg(rs)
            out = {"runs": a["runs"], "errors": a["errors"], "tokens_in": a["tokens_in"],
                   "tokens_out": a["tokens_out"], "cost_usd": a["cost_usd"]}
            if with_avg:
                out["avg_run_ms"] = a["avg_run_ms"]
            return out

        by_agent = []
        for (aid, aname), rs in group(lambda r: (r["agent_id"], r["agent_name"])).items():
            by_agent.append({"agent_id": aid, "agent_name": aname, "runtime": rs[0]["runtime"], **entry(rs)})
        by_agent.sort(key=lambda e: (-e["runs"], e["agent_name"] or ""))
        by_model = []
        for (model, runtime), rs in group(lambda r: (r["model"], r["runtime"])).items():
            by_model.append({"model": model, "runtime": runtime, **entry(rs)})
        by_model.sort(key=lambda e: (-e["runs"], e["model"] or ""))
        by_runtime = []
        for runtime, rs in group(lambda r: r["runtime"]).items():
            by_runtime.append({"runtime": runtime, **entry(rs, with_avg=False)})
        by_runtime.sort(key=lambda e: (-e["runs"], e["runtime"] or ""))

        return {"window_s": window_s, "now": now, "kpis": kpis, "series": series,
                "by_agent": by_agent, "by_model": by_model, "by_runtime": by_runtime}

    # ------------------------------------------------------------------- demo
    def has_demo_agents(self) -> bool:
        with self._lock:
            return bool(self._db.execute("SELECT 1 FROM agents WHERE demo=1 LIMIT 1").fetchone())

    def delete_demo_data(self) -> None:
        """Remove demo agents, their edges and every demo run (extra helper).

        Publishes the per-object delete events and `demo.cleared`.
        """
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM agents WHERE demo=1")]
            for aid in ids:
                self.delete_agent(aid)
            roots = [r[0] for r in self._db.execute(
                "SELECT DISTINCT root_run_id FROM runs WHERE demo=1")]
            if roots:
                self._delete_trees(roots)
            self._pub("demo.cleared", {})
