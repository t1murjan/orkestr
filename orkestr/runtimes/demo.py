"""Demo runtime: a believable agent simulator. No network models, no subprocesses.

For a task it emits a session, thinking, streamed Russian text, several tool
calls with realistic inputs/outputs and timings, usage events with a growing
context and a result (about 7% of runs fail). When `spec.mcp_url` is set it acts
like a real MCP client: it speaks JSON-RPC over HTTP to that endpoint, calls
`list_subordinates` and delegates one to three sub-tasks through `delegate`
(sometimes in parallel, sometimes with the tool_start event arriving after the
MCP request), so the real delegation path is exercised end to end.

`speed` divides every pause (tests and the e2e script use a high speed);
`fail_rate` is the probability of a fatal failure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from typing import Any, AsyncIterator

import httpx

from .base import ModelInfo, RunSpec, RuntimeAdapter, RuntimeEvent, RuntimeInfo


class _HideRunTokens(logging.Filter):
    """httpx logs request URLs at INFO; a run's MCP URL carries its token, keep it out of logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/mcp/run/" not in record.getMessage()


logging.getLogger("httpx").addFilter(_HideRunTokens())

MODELS = [
    ModelInfo("demo-local", "Демо · локальная 8B", "lmstudio", "local", 32_768),
    ModelInfo("demo-local-large", "Демо · локальная 32B", "llamacpp", "local", 65_536),
    ModelInfo("demo-cloud", "Демо · облачная", "anthropic", "cloud", 200_000),
]
_WINDOWS = {m.id: m.context_window or 32_768 for m in MODELS}

_FILES = ["src/app.py", "src/api/routes.py", "src/services/orders.py", "src/db/models.py", "web/src/App.tsx",
          "web/src/components/Table.tsx", "tests/test_api.py", "tests/test_orders.py", "docs/README.md",
          "pyproject.toml", "package.json", "src/config.py"]
_SNIPPETS = {
    "py": "import logging\n\nlog = logging.getLogger(__name__)\n\n\ndef handle(request):\n    data = request.json()\n    log.info('got %s', data)\n    return process(data)\n",
    "tsx": "export function App() {\n  const [rows, setRows] = useState<Row[]>([])\n  useEffect(() => { load().then(setRows) }, [])\n  return <Table rows={rows} />\n}\n",
    "md": "# Проект\n\nКраткое описание, запуск и тесты.\n",
    "toml": "[project]\nname = \"app\"\nversion = \"0.3.1\"\n",
    "json": "{\n  \"name\": \"app\",\n  \"scripts\": { \"build\": \"vite build\" }\n}\n",
}
_PATTERNS = ["TODO", "def handle", "useState", "timeout", "import requests", "class Order", "logger"]
_COMMANDS = [("pytest -q", "Запускает тесты"), ("npm run build", "Собирает фронтенд"),
             ("ruff check .", "Проверяет стиль кода"), ("git status --short", "Смотрит изменения")]
_FAILURES = ["Демо: модель недоступна (таймаут соединения)", "Демо: превышен лимит запросов к модели",
             "Демо: контекст модели переполнен"]
_THINK = ["Разберу задачу и определю, что нужно сделать в первую очередь.",
          "Сначала посмотрю существующий код, затем внесу минимальные изменения.",
          "Нужно учесть ограничения проекта и не сломать тесты."]


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def _role_kind(spec: RunSpec) -> str:
    text = f"{spec.agent.get('name', '')} {spec.agent.get('role', '')}".lower()
    for key, kind in (("тест", "qa"), ("qa", "qa"), ("рецен", "review"), ("ревью", "review"), ("review", "review"),
                      ("архитект", "arch"), ("фронт", "dev"), ("бэк", "dev"), ("разработ", "dev")):
        if key in text:
            return kind
    return "generic"


def _short(text: str, n: int = 90) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1].rstrip() + "…"


def _parse_rpc(resp: httpx.Response) -> dict[str, Any]:
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError("пустой ответ SSE")
    return resp.json()


class DemoAdapter(RuntimeAdapter):
    id = "demo"
    label = "Демо"

    def __init__(self, speed: float = 1.0, fail_rate: float = 0.07) -> None:
        self.speed = speed
        self.fail_rate = fail_rate

    async def detect(self, local_servers: list[dict[str, Any]] | None = None) -> RuntimeInfo:
        return RuntimeInfo(id=self.id, label=self.label, available=True, version="демо 1.0", path=None,
                           error=None, models=list(MODELS))

    # -- helpers ---------------------------------------------------------
    async def _pause(self, lo: float, hi: float, rng: random.Random) -> None:
        await asyncio.sleep(rng.uniform(lo, hi) / max(self.speed, 0.001))

    async def _stream(self, kind: str, text: str, rng: random.Random) -> AsyncIterator[RuntimeEvent]:
        words = text.split(" ")
        i = 0
        while i < len(words):
            n = rng.randint(1, 3)
            chunk = " ".join(words[i:i + n]) + (" " if i + n < len(words) else "")
            yield RuntimeEvent(kind, {"text": chunk, "delta": True})
            i += n
            await self._pause(0.02, 0.09, rng)

    # -- the run ---------------------------------------------------------
    async def run(self, spec: RunSpec) -> AsyncIterator[RuntimeEvent]:  # type: ignore[override]
        rng = random.Random()
        model = spec.model or "demo-cloud"
        window = _WINDOWS.get(model, 32_768)
        cloud = model == "demo-cloud"
        state = {"ctx": rng.randint(2_000, 3_500), "cost": 0.0, "turns": 0}
        kind = _role_kind(spec)
        will_fail = rng.random() < self.fail_rate

        def usage(extra_ctx: int | None = None) -> RuntimeEvent:
            state["turns"] += 1
            inp = rng.randint(1_500, 6_000)
            out = rng.randint(150, 900)
            cache_r = int(state["ctx"] * rng.uniform(0.5, 0.9)) if state["turns"] > 1 else 0
            cache_w = rng.randint(200, 1_200)
            state["ctx"] = min(int(window * 0.92), state["ctx"] + out + (extra_ctx or rng.randint(600, 4_000)))
            cost = (inp * 3e-6 + out * 15e-6 + cache_r * 0.3e-6 + cache_w * 3.75e-6) if cloud else 0.0
            state["cost"] += cost
            return RuntimeEvent("usage", {
                "input_tokens": inp, "output_tokens": out, "cache_read_tokens": cache_r,
                "cache_write_tokens": cache_w, "context_used": state["ctx"], "context_window": window,
                "cost_usd": round(cost, 6)})

        yield RuntimeEvent("session", {"session_id": "demo-" + uuid.uuid4().hex[:10], "model": model,
                                       "context_window": window})
        await self._pause(0.3, 0.9, rng)
        async for ev in self._stream("thinking", rng.choice(_THINK), rng):
            yield ev
        yield usage()

        task_short = _short(spec.task, 120)
        async for ev in self._stream("text", f"Принял задачу: «{task_short}». Приступаю к работе.", rng):
            yield ev

        delegated: list[tuple[str, str]] = []
        failed_point = rng.choice(["early", "late"]) if will_fail else ""

        if spec.mcp_url:
            async for ev in self._delegate_flow(spec, rng, task_short, delegated, usage):
                yield ev

        n_tools = rng.randint(1, 2) if delegated else rng.randint(2, 5)
        files_touched: list[str] = []
        for i in range(n_tools):
            if will_fail and failed_point == "late" and i == n_tools - 1:
                await self._pause(0.5, 1.2, rng)
                yield RuntimeEvent("error", {"message": rng.choice(_FAILURES), "fatal": True})
                return
            async for ev in self._own_tool(spec, rng, kind, i, n_tools, files_touched):
                yield ev
            yield usage()
        if will_fail and failed_point == "early":
            await self._pause(0.5, 1.2, rng)
            yield RuntimeEvent("error", {"message": rng.choice(_FAILURES), "fatal": True})
            return

        summary = self._summary(spec, kind, files_touched, delegated)
        yield RuntimeEvent("text", {"text": "\n\n", "delta": True})
        async for ev in self._stream("text", summary, rng):
            yield ev
        yield usage()
        await self._pause(0.1, 0.4, rng)
        yield RuntimeEvent("result", {"text": summary, "is_error": False,
                                      "cost_usd": round(state["cost"], 6), "num_turns": state["turns"]})

    # -- own tools -------------------------------------------------------
    def _tool_plan(self, rng: random.Random, kind: str, i: int, n: int, cwd: str) -> tuple[str, dict[str, Any], str]:
        f = rng.choice(_FILES)
        full = f"{cwd.rstrip('/')}/{f}"
        order = {"review": ["Read", "Grep", "Read", "Grep", "Read"],
                 "qa": ["Read", "Bash", "Edit", "Bash", "Read"],
                 "arch": ["Glob", "Read", "Grep", "Read", "Write"],
                 "dev": ["Read", "Grep", "Edit", "Bash", "Edit"]}.get(kind, ["Read", "Grep", "Edit", "Bash", "Read"])
        name = order[i % len(order)]
        if name == "Read":
            return name, {"file_path": full}, _SNIPPETS.get(_ext(f), _SNIPPETS["py"])
        if name == "Grep":
            p = rng.choice(_PATTERNS)
            hits = "\n".join(f"{rng.choice(_FILES)}:{rng.randint(3, 180)}: {p}" for _ in range(rng.randint(1, 4)))
            return name, {"pattern": p, "path": cwd}, hits
        if name == "Glob":
            ext = rng.choice(["py", "tsx", "md"])
            return name, {"pattern": f"**/*.{ext}"}, "\n".join(x for x in _FILES if x.endswith(ext)) or "(пусто)"
        if name == "Edit":
            return name, {"file_path": full, "old_string": "return process(data)",
                          "new_string": "return process(validate(data))"}, f"Файл {full} изменён."
        if name == "Write":
            return name, {"file_path": f"{cwd.rstrip('/')}/docs/design.md",
                          "content": "# Архитектура\n\nОписание модулей и потоков данных.\n"}, "Файл создан."
        cmd, desc = rng.choice(_COMMANDS)
        out = {"pytest -q": f"{rng.randint(8, 40)} passed in {rng.uniform(0.8, 6):.2f}s",
               "npm run build": "vite v5 building for production...\n✓ 42 modules transformed.",
               "ruff check .": "All checks passed!", "git status --short": " M src/app.py\n?? docs/design.md"}[cmd]
        return "Bash", {"command": cmd, "description": desc}, out

    async def _own_tool(self, spec: RunSpec, rng: random.Random, kind: str, i: int, n: int,
                        touched: list[str]) -> AsyncIterator[RuntimeEvent]:
        name, inp, out = self._tool_plan(rng, kind, i, n, spec.cwd)
        tid = "tu_" + uuid.uuid4().hex[:10]
        yield RuntimeEvent("tool_start", {"tool_id": tid, "name": name, "input": inp, "mcp": None})
        await self._pause(0.7, 2.6, rng)
        is_err = name == "Bash" and rng.random() < 0.12
        if is_err:
            out = "FAILED tests/test_orders.py::test_total - AssertionError: 120 != 125\n1 failed, 14 passed"
        if name in ("Edit", "Write") and "file_path" in inp:
            touched.append(inp["file_path"].replace(spec.cwd.rstrip("/") + "/", ""))
        yield RuntimeEvent("tool_end", {"tool_id": tid, "output": out, "is_error": is_err})
        if rng.random() < 0.5:
            comment = "Проверяю результат." if not is_err else "Тесты упали, разберусь с причиной."
            async for ev in self._stream("text", comment, rng):
                yield ev

    def _summary(self, spec: RunSpec, kind: str, touched: list[str], delegated: list[tuple[str, str]]) -> str:
        task = _short(spec.task, 100)
        if delegated:
            lines = [f"Работа по задаче «{task}» выполнена командой."]
            for who, res in delegated:
                lines.append(f"— {who}: {_short(res, 140)}")
            return "\n".join(lines)
        files = ", ".join(sorted(set(touched))) if touched else "изменений в файлах не потребовалось"
        verb = {"review": "Проверил код", "qa": "Проверил поведение и тесты", "arch": "Проработал структуру"}.get(kind, "Выполнил работу")
        return f"{verb} по задаче «{task}». Затронуто: {files}. Замечаний, блокирующих слияние, нет."

    # -- delegation through the real MCP endpoint ---------------------------
    async def _delegate_flow(self, spec: RunSpec, rng: random.Random, task_short: str,
                             delegated: list[tuple[str, str]], usage: Any) -> AsyncIterator[RuntimeEvent]:
        assert spec.mcp_url
        server = spec.mcp_server_name
        rpc_id = 0

        async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as http:
            async def rpc(method: str, params: dict[str, Any] | None = None, notify: bool = False) -> dict[str, Any]:
                nonlocal rpc_id
                body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
                if params is not None:
                    body["params"] = params
                if not notify:
                    rpc_id += 1
                    body["id"] = rpc_id
                resp = await http.post(spec.mcp_url, json=body, headers={
                    "accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-06-18"})
                resp.raise_for_status()
                return {} if notify else _parse_rpc(resp)

            async def call_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
                try:
                    msg = await rpc("tools/call", {"name": name, "arguments": args})
                except Exception as e:
                    return f"Не удалось вызвать инструмент: {e}", True
                if "error" in msg:
                    return str(msg["error"].get("message")), True
                res = msg.get("result", {})
                text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
                return text, bool(res.get("isError"))

            try:
                init = await rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "orkestr-demo", "version": "1"}})
                if "error" in init:
                    raise RuntimeError(init["error"].get("message"))
                await rpc("notifications/initialized", notify=True)
            except Exception as e:
                yield RuntimeEvent("error", {"message": f"Демо: не удалось подключиться к MCP: {e}", "fatal": False})
                return

            def mcp_info(tool: str) -> dict[str, str]:
                return {"server": server, "tool": tool}

            tid = "tu_" + uuid.uuid4().hex[:10]
            yield RuntimeEvent("tool_start", {"tool_id": tid, "name": f"mcp__{server}__list_subordinates",
                                              "input": {}, "mcp": mcp_info("list_subordinates")})
            await self._pause(0.2, 0.6, rng)
            text, is_err = await call_tool("list_subordinates", {})
            yield RuntimeEvent("tool_end", {"tool_id": tid, "output": text, "is_error": is_err})
            yield usage()
            try:
                subs = json.loads(text) if not is_err else []
            except ValueError:
                subs = []
            if not subs:
                return

            picked = rng.sample(subs, k=min(len(subs), rng.randint(1, 3)))
            names = ", ".join(f"«{s['name']}»" for s in picked)
            async for ev in self._stream("text", f"Разделю работу: поручу части задачи ({names}).", rng):
                yield ev

            queue: asyncio.Queue[RuntimeEvent | None] = asyncio.Queue()
            parallel = len(picked) > 1 and rng.random() < 0.6
            workers: list[asyncio.Task[None]] = []

            async def worker(sub: dict[str, Any], early: bool) -> None:
                subtask = (f"Часть задачи «{task_short}» для роли «{sub.get('role') or sub['name']}». "
                           f"Выполни свою часть и коротко опиши итог.")
                tool_id = "tu_" + uuid.uuid4().hex[:10]
                start = RuntimeEvent("tool_start", {
                    "tool_id": tool_id, "name": f"mcp__{server}__delegate",
                    "input": {"agent": sub["name"], "task": subtask}, "mcp": mcp_info("delegate")})
                try:
                    if early:
                        await queue.put(start)
                        out, err = await call_tool("delegate", {"agent": sub["name"], "task": subtask})
                    else:
                        call = asyncio.ensure_future(call_tool("delegate", {"agent": sub["name"], "task": subtask}))
                        await asyncio.sleep(rng.uniform(0.05, 0.25) / max(self.speed, 0.001))
                        await queue.put(start)
                        out, err = await call
                    # a long task: poll with check_task
                    for _ in range(60):
                        try:
                            info = json.loads(out)
                        except ValueError:
                            break
                        if err or not isinstance(info, dict) or info.get("status") != "running":
                            break
                        cid = "tu_" + uuid.uuid4().hex[:10]
                        await queue.put(RuntimeEvent("tool_start", {
                            "tool_id": cid, "name": f"mcp__{server}__check_task",
                            "input": {"task_id": info["task_id"], "wait_s": 30}, "mcp": mcp_info("check_task")}))
                        out, err = await call_tool("check_task", {"task_id": info["task_id"], "wait_s": 30})
                        await queue.put(RuntimeEvent("tool_end", {"tool_id": cid, "output": out[:500], "is_error": err}))
                    await queue.put(RuntimeEvent("tool_end", {"tool_id": tool_id, "output": out, "is_error": err}))
                    delegated.append((sub["name"], out if not err else f"ошибка: {out}"))
                finally:
                    await queue.put(None)

            try:
                if parallel:
                    for s in picked:
                        workers.append(asyncio.create_task(worker(s, rng.random() < 0.7)))
                    left = len(workers)
                    while left:
                        item = await queue.get()
                        if item is None:
                            left -= 1
                        else:
                            yield item
                else:
                    for s in picked:
                        w = asyncio.create_task(worker(s, rng.random() < 0.7))
                        workers.append(w)
                        while True:
                            item = await queue.get()
                            if item is None:
                                break
                            yield item
                        await w
                for w in workers:
                    await w
                yield usage()
            finally:
                for w in workers:
                    w.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
