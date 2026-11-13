"""Short Russian "what is the agent doing now" phrases for tool calls.

`describe_tool(name, input, mcp)` is pure and never raises. Tool names and input
keys follow Claude Code (Read, Edit, Bash, Task ...) and OpenCode (read, edit,
bash, task ...); anything else falls back to a generic phrase.
"""
from __future__ import annotations

from typing import Any

_MAX_DETAIL = 60


def short_path(path: Any) -> str:
    """Last two path segments: /home/u/proj/src/app.py -> src/app.py."""
    if not isinstance(path, str) or not path.strip():
        return ""
    parts = [p for p in path.strip().replace("\\", "/").split("/") if p]
    return "/".join(parts[-2:])


def _clip(text: Any, limit: int = _MAX_DETAIL) -> str:
    if not isinstance(text, str):
        return ""
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def _first(inp: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = inp.get(k)
        if v not in (None, "", []):
            return v
    return None


def _host(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0][:_MAX_DETAIL]


def _with(prefix: str, detail: str, fallback: str) -> str:
    return f"{prefix} {detail}" if detail else fallback


def _mcp_parts(name: str, mcp: dict[str, Any] | None) -> tuple[str, str] | None:
    if isinstance(mcp, dict) and mcp.get("tool"):
        return str(mcp.get("server") or ""), str(mcp["tool"])
    if name.startswith("mcp__"):
        rest = name[5:]
        server, _, tool = rest.partition("__")
        if tool:
            return server, tool
    return None


def is_delegate_tool(name: str, mcp: dict[str, Any] | None) -> bool:
    parts = _mcp_parts(name, mcp)
    return bool(parts and parts[1] == "delegate" and parts[0] in ("orkestr", ""))


def delegate_target(inp: dict[str, Any] | None) -> str:
    if isinstance(inp, dict):
        v = inp.get("agent")
        if isinstance(v, str):
            return v.strip()
    return ""


def describe_tool(name: str, input: dict[str, Any] | None, mcp: dict[str, Any] | None = None) -> str:
    inp: dict[str, Any] = input if isinstance(input, dict) else {}
    name = name or ""
    parts = _mcp_parts(name, mcp)
    if parts:
        server, tool = parts
        if server in ("orkestr", "") and tool == "delegate":
            target = delegate_target(inp)
            return f"Поручает задачу: {target}" if target else "Поручает задачу подчинённому"
        if server in ("orkestr", "") and tool == "list_subordinates":
            return "Смотрит список подчинённых"
        if server in ("orkestr", "") and tool == "check_task":
            return "Проверяет поручённую задачу"
        return f"Вызывает инструмент {server}/{tool}" if server else f"Вызывает инструмент {tool}"

    key = name.strip().lower()
    path = short_path(_first(inp, "file_path", "filePath", "path", "notebook_path"))

    if key == "read":
        return _with("Читает", path, "Читает файл")
    if key in ("edit", "multiedit"):
        return _with("Редактирует", path, "Редактирует файл")
    if key == "write":
        return _with("Создаёт файл", path, "Записывает файл")
    if key == "patch":
        return _with("Применяет изменения в", path, "Применяет патч")
    if key == "notebookedit":
        return _with("Редактирует ноутбук", path, "Редактирует ноутбук")
    if key == "glob":
        pat = _clip(_first(inp, "pattern"))
        return _with("Ищет файлы по шаблону", pat, "Ищет файлы")
    if key == "grep":
        pat = _clip(_first(inp, "pattern", "query"))
        return _with("Ищет в коде:", f"«{pat}»" if pat else "", "Ищет в коде")
    if key == "list":
        return _with("Просматривает папку", path, "Просматривает папку")
    if key == "bash":
        cmd = _clip(_first(inp, "description", "command"))
        return _with("Выполняет команду:", cmd, "Выполняет команду")
    if key == "webfetch":
        host = _host(_first(inp, "url"))
        return _with("Загружает страницу", host, "Загружает страницу")
    if key == "websearch":
        q = _clip(_first(inp, "query"))
        return _with("Ищет в интернете:", q, "Ищет в интернете")
    if key in ("task", "agent"):
        d = _clip(_first(inp, "description", "subagent_type", "prompt"))
        return _with("Запускает помощника:", d, "Запускает помощника")
    if key == "todowrite":
        return "Обновляет список дел"
    return f"Использует инструмент {name}" if name else "Работает"
