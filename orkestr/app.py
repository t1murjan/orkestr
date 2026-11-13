"""Application factory: wires store, bus, engine, probes, MCP, watcher, demo and the web UI.

Sibling modules (engine, probes, mcp_server, watcher, demo) are imported
defensively: whatever fails to import or start is logged, recorded in
`app.state.modules` (see GET /api/v1/health) and the rest keeps serving.

Injection for tests: `engine`, `probes`, `watcher`, `driver` and `mcp` accept a
ready object, or `False` to disable that part; `None` (default) builds the real
one. `store` and `bus` can be injected as well.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from . import __version__
from .api import install_error_handlers, router
from .bus import EventBus
from .config import Config
from .store import SqliteStore

log = logging.getLogger("orkestr.app")

STOP_TIMEOUT_S = 10.0
_NO_CACHE_SUFFIXES = (".html", ".js", ".mjs", ".css")


class SpaStaticFiles(StaticFiles):
    """Static files with `index.html` fallback and no-cache for html/js/css."""

    async def get_response(self, path: str, scope: Any):
        try:
            resp = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or path.split("/", 1)[0] in ("api", "mcp"):
                raise
            index = os.path.join(str(self.directory or ""), "index.html")
            if not self.directory or not os.path.isfile(index):
                raise
            resp = FileResponse(index)
        if path.lower().endswith(_NO_CACHE_SUFFIXES) or path in ("", ".", "index.html") \
                or resp.headers.get("content-type", "").startswith("text/html"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


class _McpUnavailable:
    """ASGI stand-in mounted at /mcp when the MCP module cannot be built."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            return
        resp = JSONResponse({"error": {"code": "runtime_unavailable",
                                       "message": "MCP-сервер делегирования недоступен: модуль не загружен"}},
                            status_code=409)
        await resp(scope, receive, send)


def _try(modules: dict[str, str], name: str, fn: Any) -> Any:
    try:
        obj = fn()
        modules[name] = "ok"
        return obj
    except Exception as e:  # ImportError, SyntaxError, anything a half-written module throws
        modules[name] = f"unavailable: {e!r}"
        log.warning("module %s unavailable: %r", name, e)
        return None


def create_app(config: Config | None = None, *, store: Any = None, bus: Any = None,
               engine: Any = None, probes: Any = None, watcher: Any = None,
               driver: Any = None, mcp: Any = None) -> FastAPI:
    config = config or Config()
    modules: dict[str, str] = {}
    bus = bus or EventBus()
    store = store or SqliteStore(config.db_path, bus)

    # --- optional modules (constructed here so /mcp can be mounted; started in lifespan)
    if probes is None:
        def mk_probes() -> Any:
            from .probes.manager import ProbeManager
            return ProbeManager(store)
        probes = _try(modules, "probes", mk_probes)
    elif probes is False:
        probes = None
    if engine is None:
        def mk_engine() -> Any:
            from .engine import RunEngine
            from .runtimes import build_adapters
            return RunEngine(store, bus, build_adapters(), probes, base_url=config.base_url)
        engine = _try(modules, "engine", mk_engine)
    elif engine is False:
        engine = None
    if mcp is None and engine is not None:
        def mk_mcp() -> Any:
            from .mcp_server import build_mcp_app
            return build_mcp_app(engine, store)
        mcp = _try(modules, "mcp", mk_mcp)
    elif mcp is False:
        mcp = None
    if watcher is None and engine is not None:
        def mk_watcher() -> Any:
            from .watchers.claude_sessions import ClaudeSessionWatcher
            return ClaudeSessionWatcher(engine, store)
        watcher = _try(modules, "watcher", mk_watcher)
    elif watcher is False:
        watcher = None
    if driver is None and engine is not None:
        def mk_driver() -> Any:
            from .demo import DemoDriver
            return DemoDriver(engine, store)
        driver = _try(modules, "demo", mk_driver)
    elif driver is False:
        driver = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stops: list[tuple[str, Any]] = []
        bg: list[asyncio.Task] = []

        async def start(name: str, obj: Any) -> bool:
            try:
                await obj.start()
            except Exception as e:
                modules[name] = f"start failed: {e!r}"
                log.exception("%s failed to start", name)
                return False
            stops.append((name, obj))
            return True

        if probes is not None and not await start("probes", probes):
            app.state.probes = None
        if engine is not None:
            if await start("engine", engine):
                async def refresh() -> None:
                    try:
                        await engine.refresh_runtimes()
                    except Exception:
                        log.exception("runtime detection failed")
                bg.append(asyncio.create_task(refresh()))

                async def refresh_on_server_change() -> None:
                    # Local model lists come from the probes: when a model server
                    # goes up/down or loads another model, re-detect the runtimes
                    # so agents can pick the new local models without a restart.
                    def signature() -> tuple:
                        if probes is None:
                            return ()
                        try:
                            servers = probes.local_servers_up()
                        except Exception:
                            return ()
                        return tuple(sorted(
                            (s.get("id"), tuple(sorted(m.get("id", "") for m in (s.get("status") or {}).get("models") or [])))
                            for s in servers))
                    last = signature()
                    while True:
                        await asyncio.sleep(5)
                        cur = signature()
                        if cur != last:
                            last = cur
                            await refresh()
                bg.append(asyncio.create_task(refresh_on_server_change()))
            else:
                app.state.engine = None
        if watcher is not None:
            await start("watcher", watcher)
        if config.demo:
            try:
                from .demo import seed_demo
                res = seed_demo(store)
                if inspect.isawaitable(res):
                    await res
                store.update_settings({"demo_auto": True})
            except Exception:
                modules["demo"] = "seed failed"
                log.exception("demo seeding failed")
        if driver is not None:
            await start("demo", driver)

        mcp_ctx = None
        lifespan_ctx = getattr(getattr(mcp, "router", None), "lifespan_context", None)
        async with contextlib.AsyncExitStack() as stack:
            if lifespan_ctx is not None:
                try:
                    mcp_ctx = await stack.enter_async_context(lifespan_ctx(mcp))
                except Exception:
                    log.exception("MCP app lifespan failed")
            log.info("Оркестр %s ready; modules: %s", __version__, modules)
            try:
                yield
            finally:
                app.state.begin_shutdown()
                for t in bg:
                    t.cancel()
                for name, obj in reversed(stops):
                    try:
                        await asyncio.wait_for(obj.stop(), STOP_TIMEOUT_S)
                    except Exception:
                        log.exception("stopping %s failed", name)
                with contextlib.suppress(Exception):
                    await asyncio.gather(*bg, return_exceptions=True)
                for t in list(app.state.bg_tasks):
                    t.cancel()
        del mcp_ctx
        store.close()

    app = FastAPI(title="Оркестр", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.store = store
    app.state.bus = bus
    app.state.engine = engine
    app.state.probes = probes
    app.state.modules = modules
    app.state.bg_tasks = set()
    app.state.begin_shutdown = bus.close_all
    install_error_handlers(app)
    app.include_router(router)
    app.mount("/mcp", mcp if mcp is not None else _McpUnavailable())
    app.mount("/", SpaStaticFiles(directory=config.web_dir, html=True, check_dir=False), name="web")
    return app
