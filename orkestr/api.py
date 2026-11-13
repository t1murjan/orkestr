"""HTTP API of Оркестр: every endpoint of docs/CONTRACT.md §4 plus the SSE stream (§5).

The router reads its collaborators from `request.app.state` at call time
(`store`, `bus`, `engine`, `probes`, `config`), so optional modules that failed
to load simply answer with `runtime_unavailable` instead of breaking the app.
Errors always have the shape `{"error": {"code", "message"}}` with Russian text.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .interfaces import EngineError, StoreError

log = logging.getLogger("orkestr.api")

PING_INTERVAL_S = 15.0
FS_DIRS_LIMIT = 2000

_STATUS = {
    "not_found": 404, "validation": 422, "unknown_agent": 404, "self_edge": 409,
    "duplicate_edge": 409, "cycle": 409, "name_taken": 409, "runtime_unavailable": 409,
    "bad_cwd": 422, "depth_exceeded": 409, "empty_task": 422, "builtin": 409,
    "not_subordinate": 409,
}
_ENGINE_CODE = {"unknown_run": "not_found"}


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or _STATUS.get(code, 409)


def _error_response(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.code, exc.message, exc.status)

    @app.exception_handler(StoreError)
    async def _store(_: Request, exc: StoreError) -> JSONResponse:
        return _error_response(exc.code, str(exc) if str(exc) != exc.code else _default_msg(exc.code),
                               _STATUS.get(exc.code, 409))

    @app.exception_handler(EngineError)
    async def _engine(_: Request, exc: EngineError) -> JSONResponse:
        code = _ENGINE_CODE.get(exc.code, exc.code)
        msg = str(exc) if str(exc) != exc.code else _default_msg(code)
        return _error_response(code, msg, _STATUS.get(code, 409))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p not in ("query", "body", "path"))
        msg = f"Некорректное значение параметра «{loc}»" if loc else "Некорректный запрос"
        return _error_response("validation", msg, 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _error_response("not_found", "Не найдено", 404)
        if exc.status_code == 405:
            return _error_response("validation", "Метод не поддерживается", 405)
        return _error_response("validation", "Некорректный запрос", exc.status_code)

    @app.exception_handler(Exception)
    async def _any(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error: %s", exc)
        return _error_response("internal", "Внутренняя ошибка сервера", 500)


def _default_msg(code: str) -> str:
    return {
        "unknown_agent": "Агент не найден", "not_found": "Не найдено", "validation": "Некорректный запрос",
        "self_edge": "Агент не может быть руководителем самого себя",
        "duplicate_edge": "Такая связь уже есть", "cycle": "Связь создаёт цикл в иерархии",
        "name_taken": "Имя уже занято", "runtime_unavailable": "Среда выполнения недоступна",
        "bad_cwd": "Папка не найдена", "depth_exceeded": "Превышена максимальная глубина делегирования",
        "empty_task": "Задача не может быть пустой", "builtin": "Встроенный сервер нельзя удалить",
        "not_subordinate": "Этот агент не является подчинённым",
    }.get(code, "Ошибка")


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        raise ApiError("validation", "Тело запроса должно быть JSON-объектом") from None
    if not isinstance(data, dict):
        raise ApiError("validation", "Тело запроса должно быть JSON-объектом")
    return data


def _check_dir(path: Any, what: str = "Папка") -> str:
    if not isinstance(path, str) or not path.strip():
        raise ApiError("bad_cwd", f"{what}: путь не указан")
    p = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.isdir(p):
        raise ApiError("bad_cwd", f"{what} не найдена: {p}")
    return p


def _store(request: Request) -> Any:
    return request.app.state.store


def _engine(request: Request) -> Any:
    eng = getattr(request.app.state, "engine", None)
    if eng is None:
        raise ApiError("runtime_unavailable", "Движок запусков недоступен: модуль не загружен, подробности в журнале сервера")
    return eng


def _probes(request: Request) -> Any:
    pr = getattr(request.app.state, "probes", None)
    if pr is None:
        raise ApiError("runtime_unavailable", "Мониторинг серверов недоступен: модуль не загружен, подробности в журнале сервера")
    return pr


def _agent_or_404(store: Any, agent_id: str) -> dict[str, Any]:
    a = store.get_agent(agent_id)
    if a is None:
        raise ApiError("unknown_agent", "Агент не найден")
    return a


def _runtimes(request: Request) -> list[dict[str, Any]]:
    eng = getattr(request.app.state, "engine", None)
    if eng is None:
        return []
    try:
        return list(eng.runtimes_info())
    except Exception:
        log.exception("runtimes_info failed")
        return []


def _latest_hardware(request: Request) -> dict[str, Any] | None:
    pr = getattr(request.app.state, "probes", None)
    if pr is not None:
        try:
            s = pr.latest_hardware()
            if s:
                return s
        except Exception:
            log.exception("latest_hardware failed")
    got = _store(request).list_hardware_samples(int(time.time() * 1000) - 60_000, 2)
    return got[-1] if got else None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


router = APIRouter(prefix="/api/v1")


# ------------------------------------------------------------------ state / misc
@router.get("/state")
async def get_state(request: Request) -> dict[str, Any]:
    st = _store(request)
    return {
        "version": __version__,
        "demo_seeded": st.has_demo_agents(),
        "agents": st.list_agents(),
        "edges": st.list_edges(),
        "runtimes": _runtimes(request),
        "servers": st.list_servers(),
        "settings": st.get_settings(),
        "hardware": _latest_hardware(request),
    }


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    return {"ok": True, "version": __version__, "modules": dict(getattr(request.app.state, "modules", {}))}


@router.get("/connect")
async def connect(request: Request) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    url = f"{base}/mcp/agent/{{agent_id}}"
    snippet = json.dumps({"mcp": {"orkestr": {"type": "remote", "url": url, "enabled": True}}},
                         indent=2, ensure_ascii=False)
    return {"mcp": {
        "agent_url_template": url,
        "claude_command_template": f"claude mcp add --transport http orkestr {url}",
        "opencode_snippet_template": snippet,
        "base_url": base,
    }}


# ----------------------------------------------------------------------- agents
@router.get("/agents")
async def list_agents(request: Request) -> dict[str, Any]:
    return {"agents": _store(request).list_agents()}


@router.post("/agents")
async def create_agent(request: Request) -> dict[str, Any]:
    data = await _body(request)
    if data.get("cwd"):
        data["cwd"] = _check_dir(data["cwd"], "Папка проекта")
    return _store(request).create_agent(data)


@router.patch("/agents/{agent_id}")
async def patch_agent(agent_id: str, request: Request) -> dict[str, Any]:
    data = await _body(request)
    if data.get("cwd"):
        data["cwd"] = _check_dir(data["cwd"], "Папка проекта")
    agent = _store(request).update_agent(agent_id, data)
    if agent is None:
        raise ApiError("unknown_agent", "Агент не найден")
    return agent


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request) -> dict[str, Any]:
    st = _store(request)
    _agent_or_404(st, agent_id)
    eng = getattr(request.app.state, "engine", None)
    if eng is not None:
        for run in st.list_runs(agent_id=agent_id, status="running,queued", limit=1000):
            try:
                await eng.cancel_run(run["id"])
            except Exception:
                log.exception("cancelling run %s of deleted agent failed", run["id"])
    st.delete_agent(agent_id)
    return {"ok": True}


# ------------------------------------------------------------------------ edges
@router.get("/edges")
async def list_edges(request: Request) -> dict[str, Any]:
    return {"edges": _store(request).list_edges()}


@router.post("/edges")
async def create_edge(request: Request) -> dict[str, Any]:
    data = await _body(request)
    sup, sub = data.get("supervisor_id"), data.get("subordinate_id")
    if not isinstance(sup, str) or not isinstance(sub, str) or not sup or not sub:
        raise ApiError("validation", "Нужны supervisor_id и subordinate_id")
    return _store(request).create_edge(sup, sub)


@router.delete("/edges/{edge_id}")
async def delete_edge(edge_id: str, request: Request) -> dict[str, Any]:
    if not _store(request).delete_edge(edge_id):
        raise ApiError("not_found", "Связь не найдена")
    return {"ok": True}


# ------------------------------------------------------------------------- runs
@router.get("/runs")
async def list_runs(
    request: Request,
    status: str | None = None,
    agent_id: str | None = None,
    origin: str | None = None,
    root_only: bool = False,
    limit: int = Query(50, ge=1, le=500),
    before: int | None = None,
) -> dict[str, Any]:
    runs = _store(request).list_runs(status=status, agent_id=agent_id, origin=origin,
                                     root_only=root_only, limit=limit, before=before)
    return {"runs": runs}


@router.post("/runs")
async def create_run(request: Request) -> dict[str, Any]:
    data = await _body(request)
    task = data.get("task")
    agent_id = data.get("agent_id")
    if not isinstance(task, str) or not task.strip():
        raise ApiError("empty_task", "Опишите задачу для агента")
    if not isinstance(agent_id, str) or not agent_id:
        raise ApiError("validation", "Не указан agent_id")
    st = _store(request)
    _agent_or_404(st, agent_id)
    cwd = _check_dir(data["cwd"], "Папка запуска") if data.get("cwd") else None
    eng = _engine(request)
    return await eng.start_run(agent_id, task.strip(), cwd=cwd, origin="user")


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    st = _store(request)
    run = st.get_run(run_id)
    if run is None:
        raise ApiError("not_found", "Запуск не найден")
    tree = st.run_tree(run["root_run_id"]) or [run]
    return {"run": run, "tree": tree, "steps": st.list_steps([r["id"] for r in tree])}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    if _store(request).get_run(run_id) is None:
        raise ApiError("not_found", "Запуск не найден")
    await _engine(request).cancel_run(run_id)
    return {"ok": True}


@router.get("/overview")
async def overview(request: Request, window_s: int = Query(3600, ge=1, le=90 * 86400),
                   buckets: int = Query(60, ge=1, le=500)) -> dict[str, Any]:
    return _store(request).overview(window_s=window_s, buckets=buckets)


# --------------------------------------------------------------------- runtimes
@router.get("/runtimes")
async def get_runtimes(request: Request) -> dict[str, Any]:
    return {"runtimes": _runtimes(request)}


@router.post("/runtimes/refresh")
async def refresh_runtimes(request: Request) -> dict[str, Any]:
    eng = _engine(request)
    return {"runtimes": list(await eng.refresh_runtimes())}


# ---------------------------------------------------------------------- servers
@router.get("/servers")
async def list_servers(request: Request) -> dict[str, Any]:
    return {"servers": _store(request).list_servers()}


@router.post("/servers")
async def create_server(request: Request) -> dict[str, Any]:
    data = await _body(request)
    server = _store(request).create_server(data)
    pr = getattr(request.app.state, "probes", None)
    if pr is not None:
        _spawn(request.app, pr.check_server(server["id"]))
    return server


@router.patch("/servers/{server_id}")
async def patch_server(server_id: str, request: Request) -> dict[str, Any]:
    data = await _body(request)
    server = _store(request).update_server(server_id, data)
    if server is None:
        raise ApiError("not_found", "Сервер не найден")
    return server


@router.delete("/servers/{server_id}")
async def delete_server(server_id: str, request: Request) -> dict[str, Any]:
    if not _store(request).delete_server(server_id):
        raise ApiError("not_found", "Сервер не найден")
    return {"ok": True}


@router.post("/servers/{server_id}/check")
async def check_server(server_id: str, request: Request) -> dict[str, Any]:
    st = _store(request)
    if st.get_server(server_id) is None:
        raise ApiError("not_found", "Сервер не найден")
    server = await _probes(request).check_server(server_id)
    if server is None:
        raise ApiError("not_found", "Сервер не найден")
    return server


def _spawn(app: FastAPI, coro: Any) -> None:
    async def runner() -> None:
        try:
            await coro
        except Exception:
            log.exception("background task failed")

    task = asyncio.get_running_loop().create_task(runner())
    bg: set = app.state.bg_tasks
    bg.add(task)
    task.add_done_callback(bg.discard)


# --------------------------------------------------------------------- hardware
@router.get("/hardware")
async def hardware(request: Request, window_s: int = Query(600, ge=1, le=6 * 3600)) -> dict[str, Any]:
    since = int(time.time() * 1000) - window_s * 1000
    samples = _store(request).list_hardware_samples(since, 300)
    slim = [{k: v for k, v in s.items() if k != "processes"} for s in samples]
    return {"latest": _latest_hardware(request), "samples": slim}


# --------------------------------------------------------------------- settings
@router.get("/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    return _store(request).get_settings()


@router.patch("/settings")
async def patch_settings(request: Request) -> dict[str, Any]:
    data = await _body(request)
    if data.get("default_cwd"):
        data["default_cwd"] = _check_dir(data["default_cwd"], "Папка по умолчанию")
    return _store(request).update_settings(data)


# ---------------------------------------------------------------------- fs/dirs
@router.get("/fs/dirs")
async def fs_dirs(path: str | None = None) -> dict[str, Any]:
    try:
        target = Path(path).expanduser() if path else Path.home()
        target = target.resolve()
    except (OSError, RuntimeError, ValueError):
        raise ApiError("bad_cwd", "Некорректный путь") from None
    if not target.is_dir():
        raise ApiError("bad_cwd", f"Папка не найдена: {target}")
    dirs: list[dict[str, str]] = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                    if not os.access(entry.path, os.R_OK | os.X_OK):
                        continue
                except OSError:
                    continue
                dirs.append({"name": entry.name, "path": os.path.join(str(target), entry.name)})
                if len(dirs) >= FS_DIRS_LIMIT:
                    break
    except PermissionError:
        raise ApiError("bad_cwd", f"Нет доступа к папке: {target}") from None
    except OSError as e:
        raise ApiError("bad_cwd", f"Не удалось прочитать папку: {e.strerror or target}") from None
    dirs.sort(key=lambda d: d["name"].casefold())
    parent = str(target.parent) if target.parent != target else None
    return {"path": str(target), "parent": parent, "dirs": dirs}


# ------------------------------------------------------------------------- demo
@router.post("/demo/seed")
async def demo_seed(request: Request) -> dict[str, Any]:
    try:
        from . import demo
        seed = demo.seed_demo
    except Exception as e:
        log.warning("demo module unavailable: %r", e)
        raise ApiError("runtime_unavailable", "Демо-режим недоступен: модуль не загружен") from None
    result = await _maybe_await(seed(_store(request)))
    return result if isinstance(result, dict) else {"agents": [], "edges": []}


@router.post("/demo/clear")
async def demo_clear(request: Request) -> dict[str, Any]:
    st = _store(request)
    eng = getattr(request.app.state, "engine", None)
    try:
        from . import demo
        clear = demo.clear_demo
    except Exception as e:
        log.warning("demo module unavailable, clearing through the store: %r", e)
        clear = None
    if clear is not None:
        await _maybe_await(clear(st, eng))
    elif eng is not None:
        for run in st.list_runs(status="running,queued", limit=1000):
            if run.get("demo"):
                try:
                    await eng.cancel_run(run["id"])
                except Exception:
                    log.exception("cancel demo run failed")
    # make the result certain regardless of what the demo module did
    if st.has_demo_agents() or clear is None:
        st.delete_demo_data()
    else:
        request.app.state.bus.publish("demo.cleared", {})
    return {"ok": True}


# ----------------------------------------------------------------------- stream
def _sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    bus = request.app.state.bus
    sub = bus.subscribe()  # subscribe before `hello` so nothing is missed

    async def gen():
        try:
            yield _sse("hello", {"version": __version__, "now": int(time.time() * 1000)})
            it = sub.__aiter__()
            pending: asyncio.Task | None = None
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(it.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=PING_INTERVAL_S)
                if not done:
                    if await request.is_disconnected():
                        break
                    yield _sse("ping", {"now": int(time.time() * 1000)})
                    continue
                task, pending = pending, None
                try:
                    etype, data = task.result()
                except StopAsyncIteration:
                    break
                yield _sse(etype, data)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
            sub.close()

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)
