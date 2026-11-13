"""MCP server (Streamable HTTP, JSON responses) for delegation.

Hand-written subset of the protocol rather than the official SDK's server: the
SDK (v2) negotiates per-request/stateful envelopes through a session manager
that has no clean way to bind an identity to the URL path, and this server is
stateless by nature (identity = the token or agent id in the path).

Served paths (mount the returned app at `/mcp`):
    POST /mcp/run/{token}       identity: an active run (token unguessable)
    POST /mcp/agent/{agent_id}  identity: an agent of the hierarchy

Implemented: initialize (version negotiation: 2025-11-25, 2025-06-18,
2025-03-26, 2024-11-05), notifications/* (202), ping, tools/list, tools/call.
`server/discover` and other methods answer JSON-RPC -32601, which makes the
official 2.x client fall back to the `initialize` handshake. GET/DELETE answer
405 (no server-initiated stream, no sessions). No Mcp-Session-Id is issued.
The tools are `list_subordinates`, `delegate`, `check_task` (CONTRACT §6).

Verified against: the official `mcp` 2.3 Python client over real HTTP
(tests/test_mcp_server.py). See that file's docstring for the Claude Code CLI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from . import __version__
from .engine import DelegationError

log = logging.getLogger("orkestr.mcp")

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_ROUTE = re.compile(r"/(run|agent)/([^/]+)/?$")
_MAX_BODY = 4 * 1024 * 1024
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_subordinates",
        "title": "Список подчинённых",
        "description": (
            "Возвращает ваших текущих подчинённых в иерархии: имя, роль, среду выполнения, модель и статус. "
            "Вызывайте перед delegate, чтобы выбрать, кому поручить работу."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "delegate",
        "title": "Поручить задачу подчинённому",
        "description": (
            "Поручает задачу подчинённому агенту и ждёт результата. Вернёт текст результата подчинённого. "
            "Если задача выполняется долго, вернёт {\"status\":\"running\",\"task_id\":...} — тогда вызывайте "
            "check_task. Формулируйте задачу самодостаточно: у подчинённого нет вашего контекста."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Имя подчинённого из list_subordinates."},
                "task": {"type": "string", "description": "Полная формулировка задачи."},
            },
            "required": ["agent", "task"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "openWorldHint": False},
    },
    {
        "name": "check_task",
        "title": "Проверить поручённую задачу",
        "description": (
            "Проверяет задачу, поручённую через delegate, по task_id. Ждёт до wait_s секунд (по умолчанию 30) "
            "и возвращает результат либо статус running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Идентификатор из ответа delegate."},
                "wait_s": {"type": "integer", "minimum": 0, "maximum": 300,
                           "description": "Сколько секунд ждать завершения (0 — не ждать)."},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]

INSTRUCTIONS = (
    "Оркестр: инструменты делегирования. list_subordinates — подчинённые, delegate — поручить задачу, "
    "check_task — узнать результат долгой задачи."
)


def _rpc_error(id_: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def _text(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _json_text(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


class _Disconnected(Exception):
    pass


async def _race_disconnect(coro: Any, receive: Any) -> Any:
    """Run `coro`; abandon it (cancel) if the HTTP client goes away first."""
    work = asyncio.ensure_future(coro)

    async def watch() -> None:
        while True:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                return

    watcher = asyncio.ensure_future(watch())
    try:
        done, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()
        raise _Disconnected
    finally:
        for t in (work, watcher):
            t.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)


class McpApp:
    def __init__(self, engine: Any, store: Any) -> None:
        self.engine = engine
        self.store = store

    # -- ASGI -----------------------------------------------------------
    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        try:
            await self._http(scope, receive, send)
        except _Disconnected:
            pass  # nobody is listening any more
        except Exception:
            log.exception("mcp request failed")
            await self._json(send, 500, _rpc_error(None, -32603, "Внутренняя ошибка сервера"))

    async def _json(self, send: Any, status: int, body: Any, extra: list[tuple[bytes, bytes]] | None = None) -> None:
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                   (b"content-length", str(len(raw)).encode())]
        headers += extra or []
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": raw})

    async def _empty(self, send: Any, status: int, extra: list[tuple[bytes, bytes]] | None = None) -> None:
        headers = [(b"content-length", b"0")] + (extra or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    async def _read_body(self, receive: Any) -> bytes | None:
        chunks: list[bytes] = []
        size = 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                raise _Disconnected
            chunk = msg.get("body", b"")
            size += len(chunk)
            if size > _MAX_BODY:
                return None
            chunks.append(chunk)
            if not msg.get("more_body"):
                return b"".join(chunks)

    async def _http(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        m = _ROUTE.search(scope.get("path", ""))
        if m is None:
            return await self._json(send, 404, _rpc_error(None, -32001, "Неизвестный адрес MCP"))
        origin = headers.get("origin")
        if origin:
            host = re.sub(r"^[a-z]+://", "", origin).split("/")[0]
            host = host.rsplit(":", 1)[0] if not host.endswith("]") and ":" in host else host
            if host not in _LOCAL_HOSTS:
                return await self._json(send, 403, _rpc_error(None, -32600, "Запрещённый источник запроса"))
        method = scope["method"].upper()
        if method != "POST":
            return await self._empty(send, 405, [(b"allow", b"POST")])

        kind, ident = m.group(1), m.group(2)
        caller = self.engine.caller_for_token(ident) if kind == "run" else self.engine.caller_for_agent(ident)
        if caller is None:
            return await self._json(send, 404, _rpc_error(None, -32001, "Сессия не найдена или уже завершена"))

        body = await self._read_body(receive)
        if body is None:
            return await self._json(send, 413, _rpc_error(None, -32600, "Слишком большой запрос"))
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return await self._json(send, 400, _rpc_error(None, -32700, "Некорректный JSON"))

        version = headers.get("mcp-protocol-version")
        if version and version not in SUPPORTED_VERSIONS:
            return await self._json(send, 400, _rpc_error(
                payload.get("id") if isinstance(payload, dict) else None, -32022,
                f"Неподдерживаемая версия протокола: {version}",
                {"supported": list(SUPPORTED_VERSIONS), "requested": version}))

        if isinstance(payload, list):
            if not payload:
                return await self._json(send, 400, _rpc_error(None, -32600, "Пустой пакет"))
            out = [r for r in [await self._message(m_, caller, receive) for m_ in payload] if r is not None]
            if not out:
                return await self._empty(send, 202)
            return await self._json(send, 200, out)
        resp = await self._message(payload, caller, receive)
        if resp is None:
            return await self._empty(send, 202)
        await self._json(send, 200, resp)

    # -- JSON-RPC ---------------------------------------------------------
    async def _message(self, msg: Any, caller: Any, receive: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Некорректный запрос JSON-RPC")
        mid = msg.get("id")
        method = msg.get("method")
        if method is None:  # a response from the client: nothing to do
            return None
        if "id" not in msg:  # notification
            return None
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            ver = asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": ver,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "orkestr", "title": "Оркестр", "version": __version__},
                "instructions": INSTRUCTIONS,
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            name = params.get("name")
            if name not in {t["name"] for t in TOOLS}:
                return _rpc_error(mid, -32602, f"Неизвестный инструмент: {name}")
            args = params.get("arguments")
            result = await self._call(name, args if isinstance(args, dict) else {}, caller, receive)
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        return _rpc_error(mid, -32601, f"Метод не поддерживается: {method}")

    async def _call(self, name: str, args: dict[str, Any], caller: Any, receive: Any) -> dict[str, Any]:
        try:
            return await _race_disconnect(self._tool(name, args, caller), receive)
        except DelegationError as e:
            return _text(str(e), True)
        except _Disconnected:
            raise
        except Exception as e:
            log.exception("tool %s failed", name)
            return _text(f"Внутренняя ошибка инструмента: {e}", True)

    async def _tool(self, name: str, args: dict[str, Any], caller: Any) -> dict[str, Any]:
        eng = self.engine
        if name == "list_subordinates":
            return _text(_json_text(await eng.mcp_list_subordinates(caller)))
        if name == "delegate":
            agent, task = args.get("agent"), args.get("task")
            if not isinstance(agent, str) or not isinstance(task, str):
                return _text("Параметры agent и task обязательны и должны быть строками.", True)
            r = await eng.mcp_delegate(caller, agent, task)
            return self._render_task(r, agent)
        if name == "check_task":
            tid, wait = args.get("task_id"), args.get("wait_s")
            if not isinstance(tid, str) or not tid:
                return _text("Параметр task_id обязателен.", True)
            if wait is not None and (isinstance(wait, bool) or not isinstance(wait, (int, float))):
                return _text("Параметр wait_s должен быть числом секунд.", True)
            r = await eng.mcp_check_task(caller, tid, wait)
            return self._render_task(r, r.get("agent") or "")
        return _text("Неизвестный инструмент", True)

    @staticmethod
    def _render_task(r: dict[str, Any], agent: str) -> dict[str, Any]:
        st = r["status"]
        if st == "running":
            return _text(_json_text({
                "status": "running", "task_id": r["task_id"],
                "message": "Задача ещё выполняется. Вызовите check_task с этим task_id, чтобы дождаться результата.",
            }))
        if st == "ok":
            return _text(r.get("result") or "(подчинённый вернул пустой ответ)")
        if st == "cancelled":
            return _text(f"Задача подчинённого «{agent}» была отменена.", True)
        return _text(f"Подчинённый «{agent}» завершил работу с ошибкой: {r.get('error') or 'неизвестная ошибка'}", True)


def build_mcp_app(engine: Any, store: Any) -> McpApp:
    """ASGI app to mount at `/mcp`."""
    return McpApp(engine, store)
