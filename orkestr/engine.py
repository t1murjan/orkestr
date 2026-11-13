"""Execution core: runs agents through runtime adapters and implements delegation.

Design notes
------------
* One asyncio task per run (`_run_main`). It waits for a concurrency slot, builds
  the RunSpec, iterates the adapter's event stream and persists everything via
  the Store (steps, counters, activity, result). `_on_event` is synchronous so
  external sessions (`external_event`) use the exact same pipeline.
* Concurrency: `settings.max_concurrent_runs` slots. A run that is blocked in a
  delegation wait (MCP `delegate` / `check_task` call in flight) RELEASES its
  slot for the duration of the wait (its CLI is idle, nothing is computing) and
  takes it back unconditionally afterwards, so the count may briefly exceed the
  limit when several parents wake up together. A child therefore can never
  deadlock behind its own waiting parent, and queued delegated runs are served
  before queued user runs.
* Delegation over MCP: the MCP layer (mcp_server.py) calls `mcp_delegate`,
  `mcp_check_task`, `mcp_list_subordinates`. The adapter's tool_start event for
  the `delegate` tool and the MCP call race; `_link_*` reconcile both orders by
  matching on run, pending delegate step and the `agent` (then `task`) argument.
* Cancellation: `cancel_run` cancels the run and every descendant task. A task
  closes the adapter generator in a `finally` (the adapter then terminates its
  subprocess group) and records status "cancelled". When a run ends by itself,
  its still-active descendants are cancelled too (their result would be lost).
* Tokens (`/mcp/run/{token}`) live only while the run is active.
* A run gets `mcp_url` only if the agent has subordinates when the run starts.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .activity import delegate_target, describe_tool, is_delegate_tool
from .interfaces import Bus, EngineError, Store
from .runtimes.base import RunSpec, RuntimeAdapter, RuntimeEvent, RuntimeInfo

log = logging.getLogger("orkestr.engine")

TERMINAL = ("ok", "error", "cancelled")
STEP_OUTPUT_LIMIT = 20_000
STEP_INPUT_LIMIT = 4_000
RESULT_LIMIT = 100_000
FLUSH_INTERVAL_S = 0.2
DETECT_TIMEOUT_S = 12.0
RESTART_MESSAGE = "Сервер был перезапущен"


def now_ms() -> int:
    return int(time.time() * 1000)


def _trunc(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    mark = "\n…[обрезано]"
    return text[: limit - len(mark)] + mark


def _trunc_input(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _trunc(value, STEP_INPUT_LIMIT)
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {k: _trunc_input(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_trunc_input(v, depth + 1) for v in value[:200]]
    return value


def _norm(s: Any) -> str:
    return " ".join(str(s or "").split()).casefold()


class DelegationError(Exception):
    """A delegation request that must be reported to the model as a tool error."""


Caller = tuple[str, str]  # ("run", run_id) | ("agent", agent_id)


@dataclass
class _Stream:
    step_id: str
    kind: str
    started_at: int = 0
    text: str = ""
    last_flush: float = 0.0
    handle: asyncio.TimerHandle | None = None
    dirty: bool = False


@dataclass
class _ToolRec:
    step_id: str
    started_at: int
    delegation: bool = False


@dataclass
class _PendingStep:
    step_id: str
    tool_id: str
    agent: str
    task: str


@dataclass
class _PendingChild:
    run_id: str
    agent: str
    task: str


@dataclass(eq=False)
class _Ctx:
    run_id: str
    agent_id: str | None
    external: bool = False
    parent_run_id: str | None = None
    depth: int = 0
    cwd: str = ""
    model: str | None = None
    started_at: int = 0
    task: asyncio.Task[None] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    granted: asyncio.Event = field(default_factory=asyncio.Event)
    children: set[str] = field(default_factory=set)
    token: str | None = None
    cancel_requested: bool = False
    finished: bool = False
    acquired: bool = False
    holding: bool = False
    blocked: int = 0
    seq: int = 0
    stream: _Stream | None = None
    tools: dict[str, _ToolRec] = field(default_factory=dict)
    pending_steps: list[_PendingStep] = field(default_factory=list)
    pending_children: list[_PendingChild] = field(default_factory=list)
    mcp_server: str = "orkestr"
    last_text: str = ""
    outcome: tuple[str, str | None, str | None] | None = None
    delegated: bool = False  # origin == "delegation"
    # counters
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0
    cost_total: float | None = None
    context_used: int | None = None
    context_window: int | None = None
    num_turns: int = 0
    turns_reported: int | None = None
    tool_calls: int = 0
    delegations: int = 0

    def counters(self) -> dict[str, Any]:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cache_read_tokens": self.cache_read,
            "cache_write_tokens": self.cache_write,
            "cost_usd": round(self.cost_total if self.cost_total is not None else self.cost, 6),
            "context_used": self.context_used,
            "context_window": self.context_window,
            "num_turns": self.turns_reported if self.turns_reported is not None else self.num_turns,
            "tool_calls": self.tool_calls,
            "delegations": self.delegations,
        }


class RunEngine:
    def __init__(
        self,
        store: Store,
        bus: Bus,
        adapters: dict[str, RuntimeAdapter],
        probes: Any = None,
        *,
        base_url: str,
    ) -> None:
        self.store = store
        self.bus = bus
        self.adapters = adapters
        self.probes = probes
        self.base_url = base_url.rstrip("/")
        self._ctx: dict[str, _Ctx] = {}
        self._tokens: dict[str, str] = {}
        self._slots: set[str] = set()
        self._queue: list[str] = []
        self._ext: dict[str, str] = {}
        self._task_owner: dict[str, Caller] = {}
        self._info: dict[str, dict[str, Any]] = {}
        self._info_at: dict[str, float] = {}
        self._bg: set[asyncio.Task[Any]] = set()
        self._stopping = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._stopping = False
        self._recover_stale()
        self._spawn_bg(self.refresh_runtimes())

    def _spawn_bg(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg_done)

    def _bg_done(self, task: asyncio.Task[Any]) -> None:
        self._bg.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("background task failed: %r", task.exception())

    def _recover_stale(self) -> None:
        try:
            stale = self.store.list_runs(status="running,queued", limit=1_000_000)
        except Exception:
            log.exception("crash recovery: cannot list runs")
            return
        t = now_ms()
        ids = [r["id"] for r in stale]
        for r in stale:
            self.store.update_run(r["id"], {
                "status": "error", "error": RESTART_MESSAGE, "ended_at": t, "activity": None,
                "duration_ms": max(0, t - (r.get("started_at") or t)),
            })
        if ids:
            for s in self.store.list_steps(ids):
                if s.get("status") == "running":
                    self.store.update_step(s["id"], {
                        "status": "error", "ended_at": t,
                        "duration_ms": max(0, t - (s.get("started_at") or t)),
                    })
        if stale:
            log.info("crash recovery: %d interrupted runs marked as error", len(stale))

    async def stop(self) -> None:
        self._stopping = True
        tasks = []
        for ctx in list(self._ctx.values()):
            if ctx.external:
                self._cancel_stream_timer(ctx)
                continue
            self._request_cancel(ctx)
            if ctx.task:
                tasks.append(ctx.task)
        for t in list(self._bg):
            t.cancel()
        pending = tasks + list(self._bg)
        if pending:
            await asyncio.wait(pending, timeout=30)
        self._bg.clear()

    # ------------------------------------------------------------------
    # runtimes
    # ------------------------------------------------------------------
    def _placeholder(self, adapter: RuntimeAdapter) -> dict[str, Any]:
        return RuntimeInfo(adapter.id, adapter.label, False, error="Проверка ещё не выполнена").to_dict()

    def runtimes_info(self) -> list[dict[str, Any]]:
        return [self._info.get(rid) or self._placeholder(a) for rid, a in self.adapters.items()]

    async def _detect_one(self, rid: str, adapter: RuntimeAdapter, servers: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            info = await asyncio.wait_for(adapter.detect(servers), DETECT_TIMEOUT_S)
            d = info.to_dict()
        except asyncio.TimeoutError:
            d = RuntimeInfo(rid, adapter.label, False, error="Не удалось определить среду: превышено время ожидания").to_dict()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # adapters must not raise, but never trust that
            log.warning("detect(%s) failed: %r", rid, e)
            d = RuntimeInfo(rid, adapter.label, False, error=f"Не удалось определить среду: {e}").to_dict()
        d["id"] = rid
        self._info[rid] = d
        self._info_at[rid] = time.monotonic()
        return d

    def _local_servers(self) -> list[dict[str, Any]]:
        if self.probes is None:
            return []
        try:
            return list(self.probes.local_servers_up())
        except Exception:
            log.exception("probes.local_servers_up failed")
            return []

    async def refresh_runtimes(self) -> list[dict[str, Any]]:
        servers = self._local_servers()
        await asyncio.gather(*(self._detect_one(rid, a, servers) for rid, a in self.adapters.items()))
        result = self.runtimes_info()
        self.bus.publish("runtimes", {"runtimes": result})
        return result

    async def _ensure_runtime(self, rid: str) -> dict[str, Any]:
        adapter = self.adapters.get(rid)
        if adapter is None:
            raise EngineError("runtime_unavailable", f"Среда выполнения «{rid}» не подключена")
        info = self._info.get(rid)
        stale = rid not in self._info_at or (not (info or {}).get("available") and time.monotonic() - self._info_at[rid] > 30)
        if info is None or stale:
            info = await self._detect_one(rid, adapter, self._local_servers())
            self.bus.publish("runtimes", {"runtimes": self.runtimes_info()})
        if not info.get("available"):
            why = info.get("error") or "не найдена"
            raise EngineError("runtime_unavailable", f"Среда «{adapter.label}» недоступна: {why}")
        return info

    # ------------------------------------------------------------------
    # starting runs
    # ------------------------------------------------------------------
    async def start_run(
        self,
        agent_id: str,
        task: str,
        *,
        cwd: str | None = None,
        origin: str = "user",
        parent_run_id: str | None = None,
        parent_step_id: str | None = None,
    ) -> dict[str, Any]:
        if self._stopping:
            raise EngineError("runtime_unavailable", "Сервер останавливается")
        task = (task or "").strip()
        if not task:
            raise EngineError("empty_task", "Задача не может быть пустой")
        agent = self.store.get_agent(agent_id) if agent_id else None
        if agent is None:
            raise EngineError("unknown_agent", "Агент не найден")
        await self._ensure_runtime(agent["runtime"])
        settings = self.store.get_settings()

        depth = 0
        root_id: str | None = None
        parent_ctx: _Ctx | None = None
        if parent_run_id:
            parent = self.store.get_run(parent_run_id)
            if parent is None:
                raise EngineError("unknown_run", "Родительский запуск не найден")
            parent_ctx = self._ctx.get(parent_run_id)
            if parent_ctx is None or parent_ctx.finished or parent_ctx.cancel_requested:
                raise EngineError("unknown_run", "Родительский запуск уже завершён")
            depth = int(parent.get("depth") or 0) + 1
            root_id = parent.get("root_run_id") or parent["id"]
        if depth > int(settings.get("max_depth", 3)):
            raise EngineError("depth_exceeded", f"Превышена максимальная глубина делегирования ({settings.get('max_depth')})")

        use_cwd = cwd or agent.get("cwd") or settings.get("default_cwd") or os.getcwd()
        use_cwd = os.path.abspath(os.path.expanduser(str(use_cwd)))
        if not os.path.isdir(use_cwd):
            raise EngineError("bad_cwd", f"Папка не найдена: {use_cwd}")

        run_id = uuid.uuid4().hex[:12]
        ctx = _Ctx(
            run_id=run_id, agent_id=agent["id"], parent_run_id=parent_run_id, depth=depth,
            cwd=use_cwd, model=agent.get("model"), started_at=now_ms(),
            delegated=(origin == "delegation"),
        )
        self._pump()
        queued = bool(self._queue) or len(self._slots) >= int(settings.get("max_concurrent_runs", 4))
        data = {
            "id": run_id, "agent_id": agent["id"], "agent_name": agent["name"],
            "runtime": agent["runtime"], "model": agent.get("model"), "task": task,
            "status": "queued" if queued else "running", "origin": origin,
            "parent_run_id": parent_run_id, "root_run_id": root_id or run_id, "depth": depth,
            "parent_step_id": parent_step_id, "session_id": None, "cwd": use_cwd,
            "started_at": ctx.started_at, "ended_at": None, "duration_ms": None,
            "result": None, "error": None, "activity": "В очереди" if queued else "Запускается",
            "tokens_in": 0, "tokens_out": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "cost_usd": 0.0, "context_used": None, "context_window": None,
            "num_turns": 0, "tool_calls": 0, "delegations": 0,
            "demo": bool(agent.get("demo")),
        }
        run = self.store.create_run(data)
        if run["id"] != run_id:  # store assigned its own id
            self._rekey(ctx, run["id"], root_id is None)
            run_id = run["id"]
        self._ctx[run_id] = ctx
        if parent_ctx is not None:
            parent_ctx.children.add(run_id)
            parent_ctx.delegations += 1
            self.store.update_run(parent_ctx.run_id, {"delegations": parent_ctx.delegations})
        if queued:
            self._enqueue(ctx)
        else:
            self._take_slot(ctx)
        ctx.task = asyncio.create_task(self._run_main(ctx), name=f"orkestr-run-{run_id}")
        # a task cancelled before its first step never runs its own cleanup
        ctx.task.add_done_callback(lambda _t, c=ctx: self._finish(c, "cancelled", None, None))
        return self.store.get_run(run_id) or run

    def _rekey(self, ctx: _Ctx, new_id: str, is_root: bool) -> None:
        ctx.run_id = new_id
        if is_root:
            self.store.update_run(new_id, {"root_run_id": new_id})

    # slots -------------------------------------------------------------
    def _max_slots(self) -> int:
        try:
            return max(1, int(self.store.get_settings().get("max_concurrent_runs", 4)))
        except Exception:
            return 4

    def _enqueue(self, ctx: _Ctx) -> None:
        idx = len(self._queue)
        if ctx.delegated:
            idx = 0
            for i, rid in enumerate(self._queue):
                c = self._ctx.get(rid)
                if c is not None and not c.delegated:
                    break
                idx = i + 1
        self._queue.insert(idx, ctx.run_id)

    def _take_slot(self, ctx: _Ctx) -> None:
        self._slots.add(ctx.run_id)
        ctx.holding = True
        ctx.acquired = True
        ctx.granted.set()

    def _pump(self) -> None:
        limit = self._max_slots()
        while self._queue and len(self._slots) < limit:
            rid = self._queue.pop(0)
            ctx = self._ctx.get(rid)
            if ctx is None or ctx.finished:
                continue
            self._take_slot(ctx)
            self.store.update_run(rid, {"status": "running", "started_at": now_ms(), "activity": "Запускается"})

    def _release(self, ctx: _Ctx) -> None:
        if ctx.run_id in self._queue:
            self._queue.remove(ctx.run_id)
        if ctx.holding:
            self._slots.discard(ctx.run_id)
            ctx.holding = False
        parent = self._ctx.get(ctx.parent_run_id) if ctx.parent_run_id else None
        if parent is not None and parent.blocked > 0:
            # the parent is about to wake up and take its slot back: let it, instead of a queued stranger
            asyncio.get_running_loop().call_later(0.02, self._pump)
        else:
            self._pump()

    @contextlib.asynccontextmanager
    async def _blocked(self, run_id: str | None) -> AsyncIterator[None]:
        """The run waits on a delegation: it does not occupy a slot meanwhile."""
        ctx = self._ctx.get(run_id) if run_id else None
        counted = bool(ctx and ctx.acquired and not ctx.finished)
        if counted:
            assert ctx is not None
            ctx.blocked += 1
            if ctx.blocked == 1 and ctx.holding:
                self._slots.discard(ctx.run_id)
                ctx.holding = False
                self._pump()
        try:
            yield
        finally:
            if counted:
                assert ctx is not None
                ctx.blocked -= 1
                if ctx.blocked == 0 and not ctx.finished and not ctx.holding:
                    self._slots.add(ctx.run_id)
                    ctx.holding = True

    # ------------------------------------------------------------------
    # waiting / cancelling
    # ------------------------------------------------------------------
    async def wait_run(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        ctx = self._ctx.get(run_id)
        if ctx is not None and not ctx.finished:
            try:
                await asyncio.wait_for(ctx.done.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        run = self.store.get_run(run_id)
        if run is None:
            raise EngineError("unknown_run", "Запуск не найден")
        return run

    def _request_cancel(self, ctx: _Ctx) -> None:
        if ctx.finished or ctx.cancel_requested:
            return
        ctx.cancel_requested = True
        for cid in list(ctx.children):
            child = self._ctx.get(cid)
            if child is not None:
                self._request_cancel(child)
        if ctx.task is not None and not ctx.task.done():
            ctx.task.cancel()

    def _collect(self, ctx: _Ctx, out: list[_Ctx]) -> None:
        out.append(ctx)
        for cid in ctx.children:
            c = self._ctx.get(cid)
            if c is not None:
                self._collect(c, out)

    async def cancel_run(self, run_id: str) -> bool:
        ctx = self._ctx.get(run_id)
        if ctx is None or ctx.finished or ctx.external:
            return False
        affected: list[_Ctx] = []
        self._collect(ctx, affected)
        self._request_cancel(ctx)
        waits = [c.task for c in affected if c.task is not None]
        if waits:
            await asyncio.wait(waits, timeout=30)
        return True

    # ------------------------------------------------------------------
    # one run
    # ------------------------------------------------------------------
    def _compose_prompt(self, agent: dict[str, Any], subs: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        if (agent.get("system_prompt") or "").strip():
            parts.append(agent["system_prompt"].strip())
        role = (agent.get("role") or "").strip()
        parts.append(f"Тебя зовут «{agent['name']}»." + (f" Твоя роль: {role}" if role else ""))
        if subs:
            parts.append(
                "Ты руководишь командой. Вызови инструмент list_subordinates, чтобы увидеть своих "
                "подчинённых и их роли, и инструмент delegate (agent, task), чтобы поручить работу. "
                "Поручай подчинённым то, что подходит их роли; формулируй подзадачу самодостаточно, "
                "результат придёт в ответе инструмента. Если delegate вернул task_id, вызови check_task. "
                "Итог по всей работе собери сам."
            )
        parts.append("Отвечай на языке задачи.")
        return "\n\n".join(parts)

    def _build_spec(self, ctx: _Ctx, task: str) -> RunSpec:
        agent = self.store.get_agent(ctx.agent_id or "")
        if agent is None:
            raise EngineError("unknown_agent", "Агент удалён")
        subs = self.store.subordinates_of(agent["id"])
        mcp_url = None
        if subs:
            ctx.token = secrets.token_urlsafe(24)
            self._tokens[ctx.token] = ctx.run_id
            mcp_url = f"{self.base_url}/mcp/run/{ctx.token}"
        settings = self.store.get_settings()
        return RunSpec(
            run_id=ctx.run_id,
            agent=agent,
            task=task,
            cwd=ctx.cwd,
            model=agent.get("model"),
            system_prompt=self._compose_prompt(agent, subs),
            permissions=agent.get("permissions") or "edit",
            mcp_url=mcp_url,
            mcp_tool_timeout_s=max(900, int(settings.get("delegate_wait_s", 120)) + 300),
            local_servers=self._local_servers(),
        )

    async def _aclose(self, gen: Any) -> None:
        close = getattr(gen, "aclose", None)
        if close is None:
            return
        try:
            await asyncio.wait_for(close(), 15)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("closing adapter stream failed: %r", e)

    async def _run_main(self, ctx: _Ctx) -> None:
        outcome: tuple[str, str | None, str | None] = ("error", None, "Запуск не выполнен")
        reraise = False
        gen: Any = None
        try:
            await ctx.granted.wait()
            run = self.store.get_run(ctx.run_id) or {}
            spec = self._build_spec(ctx, run.get("task") or "")
            adapter = self.adapters[run["runtime"]]
            self.store.update_run(ctx.run_id, {"status": "running", "activity": "Запускается"})
            timeout_s = float(self.store.get_settings().get("run_timeout_s", 1800) or 0)
            gen = adapter.run(spec)
            expired = False
            try:
                async with asyncio.timeout(timeout_s if timeout_s > 0 else None) as cm:
                    async for ev in gen:
                        self._safe_event(ctx, ev)
                        if ctx.outcome is not None:
                            break
            except TimeoutError:
                if not cm.expired():
                    raise
                expired = True
            finally:
                await self._aclose(gen)
                gen = None
            if expired:
                outcome = ("error", None, f"Превышено время выполнения ({int(timeout_s)} с)")
            elif ctx.outcome is not None:
                outcome = ctx.outcome
            else:
                outcome = ("error", ctx.last_text or None, "Агент завершил работу без итогового ответа")
        except asyncio.CancelledError:
            outcome = ("cancelled", None, None)
            reraise = not ctx.cancel_requested
        except EngineError as e:
            outcome = ("error", None, str(e))
        except Exception as e:
            log.exception("run %s crashed", ctx.run_id)
            outcome = ("error", None, f"Внутренняя ошибка запуска: {e}")
        finally:
            if gen is not None:  # cancelled before/while closing
                with contextlib.suppress(BaseException):
                    await self._aclose(gen)
            self._finish(ctx, *outcome)
        if reraise:
            raise asyncio.CancelledError

    def _safe_event(self, ctx: _Ctx, ev: RuntimeEvent, ts: int | None = None) -> None:
        try:
            self._on_event(ctx, ev, ts)
        except Exception:
            log.exception("event %s of run %s could not be processed", getattr(ev, "type", "?"), ctx.run_id)

    # ------------------------------------------------------------------
    # event -> steps pipeline (sync; also used by external sessions)
    # ------------------------------------------------------------------
    def _next_seq(self, ctx: _Ctx) -> int:
        ctx.seq += 1
        return ctx.seq

    def _new_step(self, ctx: _Ctx, kind: str, name: str, title: str, ts: int, *, input: Any = None,
                  output: str | None = None, tool_id: str | None = None, status: str = "running",
                  ended: bool = False) -> dict[str, Any]:
        return self.store.create_step({
            "run_id": ctx.run_id, "seq": self._next_seq(ctx), "kind": kind, "name": name, "title": title,
            "status": status, "started_at": ts, "ended_at": ts if ended else None,
            "duration_ms": 0 if ended else None, "input": input, "output": output,
            "tool_id": tool_id, "child_run_id": None,
        })

    def _run_patch(self, ctx: _Ctx, **extra: Any) -> None:
        patch = ctx.counters()
        patch.update(extra)
        self.store.update_run(ctx.run_id, patch)

    # throttled streaming text
    def _stream_flush(self, ctx: _Ctx, st: _Stream) -> None:
        if st.handle is not None:
            st.handle.cancel()
            st.handle = None
        st.dirty = False
        st.last_flush = time.monotonic()
        self.store.update_step(st.step_id, {"output": _trunc(st.text, STEP_OUTPUT_LIMIT)})

    def _stream_timer(self, ctx: _Ctx, st: _Stream) -> None:
        st.handle = None
        if st.dirty and ctx.stream is st:
            try:
                self._stream_flush(ctx, st)
            except Exception:
                log.exception("stream flush failed")

    def _stream_append(self, ctx: _Ctx, st: _Stream, delta: str) -> None:
        if len(st.text) < STEP_OUTPUT_LIMIT * 2:
            st.text += delta
        st.dirty = True
        wait = FLUSH_INTERVAL_S - (time.monotonic() - st.last_flush)
        if wait <= 0:
            self._stream_flush(ctx, st)
        elif st.handle is None:
            st.handle = asyncio.get_running_loop().call_later(wait, self._stream_timer, ctx, st)

    def _cancel_stream_timer(self, ctx: _Ctx) -> None:
        if ctx.stream is not None and ctx.stream.handle is not None:
            ctx.stream.handle.cancel()
            ctx.stream.handle = None

    def _close_stream(self, ctx: _Ctx, ts: int, status: str = "ok", final_text: str | None = None) -> None:
        st = ctx.stream
        if st is None:
            return
        if st.handle is not None:
            st.handle.cancel()
            st.handle = None
        ctx.stream = None
        if final_text is not None:
            st.text = final_text
        if st.kind == "message" and st.text.strip():
            ctx.last_text = st.text
        self.store.update_step(st.step_id, {
            "output": _trunc(st.text, STEP_OUTPUT_LIMIT), "status": status,
            "ended_at": ts, "duration_ms": max(0, ts - st.started_at),
        })

    def _open_stream(self, ctx: _Ctx, kind: str, ts: int) -> _Stream:
        if kind == "message":
            step = self._new_step(ctx, "message", "assistant", "Отвечает", ts, output="")
        else:
            step = self._new_step(ctx, "thinking", "thinking", "Размышляет", ts, output="")
        st = _Stream(step_id=step["id"], kind=kind, started_at=ts)
        ctx.stream = st
        return st

    def _on_text(self, ctx: _Ctx, kind: str, data: dict[str, Any], ts: int) -> None:
        text = str(data.get("text") or "")
        delta = bool(data.get("delta"))
        activity = "Размышляет" if kind == "thinking" else "Пишет ответ"
        if delta:
            if not text:
                return
            if ctx.stream is None or ctx.stream.kind != kind:
                self._close_stream(ctx, ts)
                self._open_stream(ctx, kind, ts)
                self._run_patch(ctx, activity=activity)
            assert ctx.stream is not None
            self._stream_append(ctx, ctx.stream, text)
            return
        if not text.strip():
            return
        st = ctx.stream
        if st is not None and st.kind == kind and (text.startswith(st.text) or st.text.startswith(text)):
            # the complete block repeats what was streamed: finalize that step
            self._close_stream(ctx, ts, final_text=text if len(text) >= len(st.text) else None)
        else:
            self._close_stream(ctx, ts)
            self._new_step(ctx, "message" if kind == "message" else "thinking",
                                  "assistant" if kind == "message" else "thinking",
                                  "Отвечает" if kind == "message" else "Размышляет", ts,
                                  output=_trunc(text, STEP_OUTPUT_LIMIT), status="ok", ended=True)
            if kind == "message":
                ctx.last_text = text
        self._run_patch(ctx, activity=activity)

    def _on_event(self, ctx: _Ctx, ev: RuntimeEvent, ts: int | None = None) -> None:
        ts = ts or now_ms()
        d = ev.data or {}
        t = ev.type
        if t == "session":
            patch: dict[str, Any] = {}
            if d.get("session_id"):
                patch["session_id"] = d["session_id"]
            if d.get("model"):
                ctx.model = d["model"]
                patch["model"] = d["model"]
            if d.get("context_window"):
                ctx.context_window = int(d["context_window"])
                patch["context_window"] = ctx.context_window
            if patch:
                self.store.update_run(ctx.run_id, patch)
        elif t == "thinking":
            self._on_text(ctx, "thinking", d, ts)
        elif t == "text":
            self._on_text(ctx, "message", d, ts)
        elif t == "tool_start":
            self._on_tool_start(ctx, d, ts)
        elif t == "tool_end":
            self._on_tool_end(ctx, d, ts)
        elif t == "usage":
            ctx.tokens_in += int(d.get("input_tokens") or 0)
            ctx.tokens_out += int(d.get("output_tokens") or 0)
            ctx.cache_read += int(d.get("cache_read_tokens") or 0)
            ctx.cache_write += int(d.get("cache_write_tokens") or 0)
            ctx.cost += float(d.get("cost_usd") or 0.0)
            ctx.num_turns += 1
            if d.get("context_used") is not None:
                ctx.context_used = int(d["context_used"])
            if d.get("context_window"):
                ctx.context_window = int(d["context_window"])
            self._run_patch(ctx)
        elif t == "result":
            self._close_stream(ctx, ts)
            if d.get("cost_usd") is not None:
                ctx.cost_total = float(d["cost_usd"])
            if d.get("num_turns") is not None:
                ctx.turns_reported = int(d["num_turns"])
            text = _trunc(str(d.get("text") or ctx.last_text or ""), RESULT_LIMIT)
            if d.get("is_error"):
                ctx.outcome = ("error", None, text or "Агент сообщил об ошибке")
            else:
                ctx.outcome = ("ok", text, None)
        elif t == "error":
            msg = str(d.get("message") or "Ошибка")
            self._close_stream(ctx, ts)
            if d.get("fatal"):
                ctx.outcome = ("error", ctx.last_text or None, msg)
            else:
                self._new_step(ctx, "error", "error", _trunc(msg, 200), ts, output=_trunc(msg, STEP_OUTPUT_LIMIT),
                               status="error", ended=True)
        elif t == "log":
            log.debug("[%s] %s", ctx.run_id, d.get("message"))

    # tools ---------------------------------------------------------------
    def _on_tool_start(self, ctx: _Ctx, d: dict[str, Any], ts: int) -> None:
        self._close_stream(ctx, ts)
        name = str(d.get("name") or "tool")
        inp = d.get("input") if isinstance(d.get("input"), dict) else {}
        mcp = d.get("mcp") if isinstance(d.get("mcp"), dict) else None
        tool_id = str(d.get("tool_id") or f"t{ctx.seq + 1}")
        title = describe_tool(name, inp, mcp)
        delegation = is_delegate_tool(name, mcp)
        step = self._new_step(ctx, "delegation" if delegation else "tool", name, title, ts,
                              input=_trunc_input(inp), tool_id=tool_id)
        ctx.tools[tool_id] = _ToolRec(step["id"], ts, delegation)
        if not delegation:
            ctx.tool_calls += 1
        else:
            self._link_step_to_pending_child(ctx, step["id"], tool_id, inp)
        self._run_patch(ctx, activity=title)

    def _on_tool_end(self, ctx: _Ctx, d: dict[str, Any], ts: int) -> None:
        tool_id = str(d.get("tool_id") or "")
        rec = ctx.tools.pop(tool_id, None)
        if rec is None:
            return
        ctx.pending_steps = [p for p in ctx.pending_steps if p.step_id != rec.step_id]
        out = d.get("output")
        out = out if isinstance(out, str) else ("" if out is None else str(out))
        self.store.update_step(rec.step_id, {
            "status": "error" if d.get("is_error") else "ok", "ended_at": ts,
            "duration_ms": max(0, ts - rec.started_at), "output": _trunc(out, STEP_OUTPUT_LIMIT),
        })
        self._run_patch(ctx, activity="Думает")

    # delegation linking -------------------------------------------------
    def _link_step_to_pending_child(self, ctx: _Ctx, step_id: str, tool_id: str, inp: dict[str, Any]) -> None:
        """tool_start of `delegate` arrived: attach a child created earlier by the MCP call, else wait for it."""
        agent, task = delegate_target(inp), str(inp.get("task") or "")
        best: _PendingChild | None = None
        score = 0
        for pc in ctx.pending_children:
            if _norm(pc.agent) != _norm(agent):
                continue
            s = 2 if _norm(pc.task) == _norm(task) else 1
            if s > score:
                best, score = pc, s
        if best is not None:
            ctx.pending_children.remove(best)
            self._attach(step_id, best.run_id)
        else:
            ctx.pending_steps.append(_PendingStep(step_id, tool_id, agent, task))

    def _attach(self, step_id: str, child_run_id: str) -> None:
        self.store.update_step(step_id, {"child_run_id": child_run_id})
        self.store.update_run(child_run_id, {"parent_step_id": step_id})

    def _match_pending_step(self, ctx: _Ctx, agent: str, task: str) -> _PendingStep | None:
        best: _PendingStep | None = None
        score = 0
        for p in ctx.pending_steps:
            if _norm(p.agent) != _norm(agent):
                continue
            s = 2 if _norm(p.task) == _norm(task) else 1
            if s > score:
                best, score = p, s
        if best is not None:
            ctx.pending_steps.remove(best)
        return best

    def _link_child(self, ctx: _Ctx, child_run_id: str, agent: str, task: str) -> None:
        p = self._match_pending_step(ctx, agent, task)
        if p is not None:
            self._attach(p.step_id, child_run_id)
        else:
            ctx.pending_children.append(_PendingChild(child_run_id, agent, task))

    # ------------------------------------------------------------------
    # finishing
    # ------------------------------------------------------------------
    def _finish(self, ctx: _Ctx, status: str, result: str | None, error: str | None, ts: int | None = None) -> None:
        if ctx.finished:
            return
        ts = ts or now_ms()
        try:
            self._close_stream(ctx, ts, status="ok")
            step_status = "ok" if status == "ok" else "error"
            for rec in list(ctx.tools.values()):
                self.store.update_step(rec.step_id, {
                    "status": step_status, "ended_at": ts, "duration_ms": max(0, ts - rec.started_at),
                })
            ctx.tools.clear()
            self._run_patch(
                ctx, status=status, result=result, error=error, ended_at=ts,
                duration_ms=max(0, ts - ctx.started_at), activity=None,
            )
        except Exception:
            log.exception("finalizing run %s failed", ctx.run_id)
        finally:
            ctx.finished = True
            self._cancel_stream_timer(ctx)
            if ctx.token:
                self._tokens.pop(ctx.token, None)
            self._release(ctx)
            self._ctx.pop(ctx.run_id, None)
            ctx.done.set()
            for cid in list(ctx.children):
                child = self._ctx.get(cid)
                if child is not None:
                    self._request_cancel(child)
            if ctx.parent_run_id:
                parent = self._ctx.get(ctx.parent_run_id)
                if parent is not None:
                    parent.children.discard(ctx.run_id)

    # ------------------------------------------------------------------
    # MCP-facing API (called by mcp_server.py)
    # ------------------------------------------------------------------
    def caller_for_token(self, token: str) -> Caller | None:
        rid = self._tokens.get(token)
        ctx = self._ctx.get(rid) if rid else None
        if ctx is None or ctx.finished:
            return None
        return ("run", ctx.run_id)

    def caller_for_agent(self, agent_id: str) -> Caller | None:
        return ("agent", agent_id) if self.store.get_agent(agent_id) else None

    def _supervisor_id(self, caller: Caller) -> str:
        kind, ident = caller
        if kind == "agent":
            if self.store.get_agent(ident) is None:
                raise DelegationError("Агент не найден: возможно, он был удалён.")
            return ident
        ctx = self._ctx.get(ident)
        if ctx is None or ctx.finished or ctx.cancel_requested:
            raise DelegationError("Ваш запуск уже завершён.")
        if not ctx.agent_id:
            raise DelegationError("Этот запуск не привязан к агенту иерархии.")
        return ctx.agent_id

    async def mcp_list_subordinates(self, caller: Caller) -> list[dict[str, Any]]:
        sup = self._supervisor_id(caller)
        return [
            {"name": a["name"], "role": a.get("role") or "", "runtime": a["runtime"], "model": a.get("model"),
             "status": (a.get("live") or {}).get("status", "idle")}
            for a in self.store.subordinates_of(sup)
        ]

    async def mcp_delegate(self, caller: Caller, agent: str, task: str) -> dict[str, Any]:
        """Starts a subordinate's run and waits up to `delegate_wait_s`.

        Returns {"status": "ok"|"error"|"cancelled"|"running", "task_id", "result", "error"}.
        Raises DelegationError for refusals."""
        agent = (agent or "").strip()
        task = (task or "").strip()
        if not agent:
            raise DelegationError("Не указан агент (параметр agent).")
        if not task:
            raise DelegationError("Не указана задача (параметр task).")
        sup = self._supervisor_id(caller)
        subs = self.store.subordinates_of(sup)
        target = next((a for a in subs if a["name"] == agent), None) or \
            next((a for a in subs if _norm(a["name"]) == _norm(agent)), None)
        if target is None:
            names = ", ".join(f"«{a['name']}»" for a in subs)
            raise DelegationError(
                f"«{agent}» не ваш подчинённый. Доступные подчинённые: {names}." if subs
                else "У вас нет подчинённых, поручить задачу некому."
            )
        kind, ident = caller
        parent_ctx = self._ctx.get(ident) if kind == "run" else None
        wait_s = float(self.store.get_settings().get("delegate_wait_s", 120))
        pending = self._match_pending_step(parent_ctx, agent, task) if parent_ctx else None
        try:
            child = await self.start_run(
                target["id"], task, origin="delegation",
                parent_run_id=ident if kind == "run" else None,
                parent_step_id=pending.step_id if pending else None,
            )
        except EngineError as e:
            if pending is not None and parent_ctx is not None:
                parent_ctx.pending_steps.append(pending)
            raise DelegationError(self._explain(e, target)) from e
        self._remember_task(child["id"], caller)
        if parent_ctx is not None:
            if pending is not None:
                self.store.update_step(pending.step_id, {"child_run_id": child["id"]})
            else:
                self._link_child(parent_ctx, child["id"], agent, task)
        # The child is created (and queued with priority) BEFORE the parent gives up its slot,
        # so a queued stranger cannot grab the slot the child needs.
        async with self._blocked(ident if kind == "run" else None):
            final = await self.wait_run(child["id"], wait_s)
        return self._task_state(final)

    @staticmethod
    def _explain(e: EngineError, target: dict[str, Any]) -> str:
        if e.code == "depth_exceeded":
            return f"{e}. Выполните задачу самостоятельно."
        if e.code == "runtime_unavailable":
            return f"Подчинённый «{target['name']}» сейчас недоступен: {e}"
        return str(e)

    def _remember_task(self, run_id: str, caller: Caller) -> None:
        self._task_owner[run_id] = caller
        while len(self._task_owner) > 5000:
            self._task_owner.pop(next(iter(self._task_owner)))

    @staticmethod
    def _task_state(run: dict[str, Any]) -> dict[str, Any]:
        st = run["status"]
        if st in ("queued", "running"):
            return {"status": "running", "task_id": run["id"], "agent": run["agent_name"]}
        return {"status": st, "task_id": run["id"], "agent": run["agent_name"],
                "result": run.get("result"), "error": run.get("error")}

    async def mcp_check_task(self, caller: Caller, task_id: str, wait_s: float | None = None) -> dict[str, Any]:
        self._supervisor_id(caller)
        run = self.store.get_run(task_id or "")
        owned = run is not None and (
            self._task_owner.get(task_id) == caller
            or (caller[0] == "run" and run.get("parent_run_id") == caller[1])
        )
        if not owned:
            raise DelegationError(f"Задача «{task_id}» не найдена среди ваших поручений.")
        assert run is not None
        if wait_s is None:
            wait_s = 30
        wait_s = max(0.0, min(float(wait_s), 300.0))
        if run["status"] in ("queued", "running") and wait_s > 0:
            async with self._blocked(caller[1] if caller[0] == "run" else None):
                run = await self.wait_run(task_id, wait_s)
        return self._task_state(run)

    # ------------------------------------------------------------------
    # external sessions
    # ------------------------------------------------------------------
    def external_begin(self, key: str, info: dict[str, Any]) -> str:
        rid = self._ext.get(key)
        if rid is not None:
            ctx = self._ctx.get(rid)
            if ctx is not None and not ctx.finished:
                return rid
            return self._ext_reopen(rid, key)
        t = int(info.get("started_at") or now_ms())
        parent_run_id = self._ext.get(info.get("parent_key") or "") if info.get("parent_key") else None
        depth, root = 0, None
        if parent_run_id:
            parent = self.store.get_run(parent_run_id)
            if parent:
                depth, root = int(parent.get("depth") or 0) + 1, parent.get("root_run_id") or parent["id"]
            else:
                parent_run_id = None
        run_id = uuid.uuid4().hex[:12]
        runtime = info.get("runtime") or "claude_code"
        title = info.get("title") or "Сессия в терминале"
        data = {
            "id": run_id, "agent_id": None, "agent_name": info.get("agent_name") or title,
            "runtime": runtime, "model": info.get("model"), "task": title, "status": "running",
            "origin": "external", "parent_run_id": parent_run_id, "root_run_id": root or run_id,
            "depth": depth, "parent_step_id": None, "session_id": info.get("session_id"),
            "cwd": info.get("cwd"), "started_at": t, "ended_at": None, "duration_ms": None,
            "result": None, "error": None, "activity": None,
            "tokens_in": 0, "tokens_out": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "cost_usd": 0.0, "context_used": None, "context_window": None,
            "num_turns": 0, "tool_calls": 0, "delegations": 0, "demo": False,
        }
        run = self.store.create_run(data)
        if run["id"] != run_id:
            run_id = run["id"]
            if root is None:
                self.store.update_run(run_id, {"root_run_id": run_id})
        ctx = _Ctx(run_id=run_id, agent_id=None, external=True, parent_run_id=parent_run_id, depth=depth,
                   cwd=info.get("cwd") or "", model=info.get("model"), started_at=t)
        self._ctx[run_id] = ctx
        self._ext[key] = run_id
        if parent_run_id and parent_run_id in self._ctx:
            self._ctx[parent_run_id].children.add(run_id)
        return run_id

    def _ext_reopen(self, run_id: str, key: str) -> str:
        run = self.store.get_run(run_id)
        if run is None:
            self._ext.pop(key, None)
            raise EngineError("unknown_run", "Запуск не найден")
        ctx = _Ctx(run_id=run_id, agent_id=None, external=True, parent_run_id=run.get("parent_run_id"),
                   depth=int(run.get("depth") or 0), cwd=run.get("cwd") or "", model=run.get("model"),
                   started_at=int(run.get("started_at") or now_ms()))
        ctx.tokens_in, ctx.tokens_out = run.get("tokens_in", 0), run.get("tokens_out", 0)
        ctx.cache_read, ctx.cache_write = run.get("cache_read_tokens", 0), run.get("cache_write_tokens", 0)
        ctx.cost = float(run.get("cost_usd") or 0.0)
        ctx.context_used, ctx.context_window = run.get("context_used"), run.get("context_window")
        ctx.num_turns, ctx.tool_calls = run.get("num_turns", 0), run.get("tool_calls", 0)
        ctx.delegations = run.get("delegations", 0)
        steps = self.store.list_steps([run_id])
        ctx.seq = max((s.get("seq", 0) for s in steps), default=0)
        self._ctx[run_id] = ctx
        self.store.update_run(run_id, {"status": "running", "ended_at": None, "duration_ms": None, "error": None})
        return run_id

    def external_event(self, run_id: str, event: RuntimeEvent, ts: int | None = None) -> None:
        ctx = self._ctx.get(run_id)
        if ctx is None or ctx.finished or not ctx.external:
            return
        self._safe_event(ctx, event, ts)

    def external_end(self, run_id: str, status: str = "ok", ts: int | None = None) -> None:
        ctx = self._ctx.get(run_id)
        if ctx is None or ctx.finished or not ctx.external:
            return
        if status not in TERMINAL:
            status = "ok"
        self._close_stream(ctx, ts or now_ms())
        result = ctx.last_text or None
        error = None
        if ctx.outcome is not None:
            result = ctx.outcome[1] or result
            error = ctx.outcome[2]
        if status == "error" and not error:
            error = "Сессия завершилась с ошибкой"
        if status == "cancelled":
            result = None
        self._finish(ctx, status, result, error, ts)
