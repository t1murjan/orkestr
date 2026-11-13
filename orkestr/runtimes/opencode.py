"""OpenCode runtime adapter for Оркестр.

Verified against OpenCode 1.19.5 (npm ``opencode-ai``, linux-x64) in a sandbox
with scratch XDG dirs.  The opencode.ai docs host was NOT reachable from the
sandbox, so everything below was established from ``--help``, the binary's
embedded config schema / OpenAPI document (``GET /doc``) and experiments with
the real binary against ``tests/tools/mock_openai_server.py`` (port 8804) and a
real MCP round trip against ``tests/tools/opencode_mcp_stub.py`` (port 8814).

Headless mode: ``opencode serve`` + HTTP API + SSE
--------------------------------------------------
``opencode run --format json`` only emits a tool call once it is *completed*
(no running state: a 6 s delegate call showed up as a single ``tool_use`` at
+6 s) and only whole text parts (no deltas).  ``opencode serve`` streams
``message.part.updated`` (tool ``pending`` -> ``running`` -> ``completed`` /
``error``), ``message.part.delta`` text deltas, per-step tokens and cost, and
gives a blocking ``POST /session/{id}/message`` for reliable completion plus
``POST /session/{id}/abort``.  So: one ``opencode serve`` process per run, on a
free 127.0.0.1 port with a random per-run basic-auth password.

Per-run configuration, nothing on disk
--------------------------------------
* ``OPENCODE_CONFIG_CONTENT`` (inline JSON config; highest precedence of the
  file/env sources, merged over the user's global and project config, which we
  never touch).  Sets ``mcp.<name>`` (``type: remote``, ``url``,
  ``timeout`` in ms), ``experimental.mcp_timeout``, ``permission``, ``provider``,
  ``share: disabled``, ``autoupdate: false``, ``snapshot: false``.
* The system prompt goes in the ``system`` field of the message request (it is
  appended to OpenCode's own system prompt; verified in the model request).
* The same permission rules are also given to the session (``POST /session``
  ``permission``), which is evaluated after agent level rules, so a user agent
  definition cannot loosen them.
* Env: ``OPENCODE_SERVER_PASSWORD`` (random), ``OPENCODE_DISABLE_AUTOUPDATE``,
  ``OPENCODE_DISABLE_LSP_DOWNLOAD``, ``OPENCODE_DISABLE_CLAUDE_CODE_PROMPT`` and
  ``..._SKILLS`` (do not feed the Claude Code global prompt/skills to OpenCode).
  ``PWD`` is set to the cwd: OpenCode takes its project directory from ``$PWD``
  (a stale inherited PWD silently moved the sandbox boundary during tests).
* ``stdin`` is /dev/null.  ``opencode run`` (and startup in general) can block
  reading an inherited non-TTY stdin; this caused sporadic hangs in testing.

Permissions (last matching rule wins; verified with ``opencode debug agent``)
-----------------------------------------------------------------------------
Defaults contain ``ask`` for ``doom_loop``, ``external_directory`` and
``read *.env``; in ``serve`` an ``ask`` blocks until someone replies, so every
level sets those explicitly and the adapter additionally answers any
``permission.asked`` event with ``reject`` (safety net).
* ``read``: ``* deny`` + read/glob/grep/list/todowrite + the orkestr MCP tools
  (``orkestr_*``); ``.env`` files denied; edit, bash, webfetch, websearch,
  task, ... denied.  Denied tools are removed from the model's tool list; if a
  model calls one anyway it gets an error tool result
  ("Model tried to call unavailable tool ...") - no hang.
* ``edit``: as ``read`` plus ``edit: allow`` (covers write/edit/patch).
  ``external_directory: deny`` blocks paths outside the cwd (verified: a write
  to a sibling dir returns "The user has specified a rule which prevents you
  from using this specific tool call").  Shell stays denied.
* ``full``: ``* allow`` (+ external_directory/doom_loop allow); ``question``
  and plan-mode tools stay denied (nobody can answer them).
Note on "inside cwd": OpenCode treats the whole *worktree* (nearest ``.git``
ancestor, else ``/``) as the project.  Alone, ``external_directory: deny`` would
therefore let ``edit`` write anywhere in the repository (verified).  The
``edit`` level adds ``edit: {"*": deny, "<cwd relative to worktree>/*": allow}``
(OpenCode matches edit patterns against worktree-relative paths), which was
verified to allow ``cwd/sub/new.txt`` and deny a sibling folder in the same repo.
Reads are not narrowed (``read`` may read anywhere inside the worktree;
``external_directory`` blocks the rest).

Local providers
---------------
For every entry of ``spec.local_servers`` an OpenAI-compatible provider
(``@ai-sdk/openai-compatible``, ``options.baseURL = <base_url>/v1``) is injected
unless the user's own config (global, ``OPENCODE_CONFIG``, project, inherited
``OPENCODE_CONFIG_CONTENT``) already defines that provider id.  Provider id:
``lmstudio`` / ``llamacpp`` for a server on its default local port (1234 /
8080), otherwise ``<kind>-<port>`` (``openai-compat-<port>``, host slug added
for non-local hosts).  Model id stored in ``agent.model`` is
``<provider>/<model id as the server reports it>``, e.g.
``lmstudio/qwen/qwen3-coder-30b``; OpenCode splits at the first slash.
``limit.context`` comes from the server's loaded (or max) context; ``limit.output``
is ``clamp(context/4, 1024, 16384)`` because OpenCode sends it as ``max_tokens``.

Event mapping (SSE ``/event`` -> RuntimeEvent)
----------------------------------------------
* session created                      -> ``session`` (model + context window)
* part ``text`` / ``reasoning`` + deltas -> ``text`` / ``thinking`` (delta=True;
  a part without deltas is emitted once with delta=False)
* part ``tool`` pending/running         -> ``tool_start`` (once per callID)
* part ``tool`` completed / error       -> ``tool_end`` (error text is the output)
* part ``step-finish`` (one per model call) -> ``usage`` deltas; reasoning
  tokens are added to output; ``context_used`` = input + cache + output;
  ``context_window`` from the model's ``limit.context``; ``cost_usd`` is
  OpenCode's computed cost (0 for local models)
* ``retry`` part -> ``log``; ``permission.asked`` -> auto reject + ``log``
* blocking message response -> ``result`` (text of the final assistant message,
  summed cost, number of steps) or fatal ``error``.
MCP tools are named ``<server>_<tool>`` (non-alphanumerics -> ``_``), e.g.
``orkestr_delegate``; ``tool_start.name`` is that raw name and ``mcp`` is
``{"server": "orkestr", "tool": "delegate"}``.  Tool inputs also get a
``file_path`` alias next to OpenCode's ``filePath``.

Cancellation: abort the session (best effort), then SIGTERM the process group,
SIGKILL after 3 s.  Known gaps: events of sub-agent (``task`` tool) child
sessions are not mapped (only the main session); sessions of terminal-launched
OpenCode live in the SQLite DB ``$XDG_DATA_HOME/opencode/opencode.db``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

from .base import (
    ModelInfo,
    RunSpec,
    RuntimeAdapter,
    RuntimeEvent,
    RuntimeInfo,
)

log = logging.getLogger("orkestr.opencode")

INSTALL_HINT = "npm install -g opencode-ai"
NOT_INSTALLED_MSG = (
    "OpenCode не установлен. Установите командой: " + INSTALL_HINT
    + " (или curl -fsSL https://opencode.ai/install | bash)"
)
OUTPUT_LIMIT = 20_000
MAX_API_RETRIES = 3     # OpenCode retries failed model calls forever with growing backoff
LOCAL_KINDS = ("lmstudio", "llamacpp", "openai_compat")
_DEFAULT_PORTS = {"lmstudio": 1234, "llamacpp": 8080}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}
_SERVER_USER = "opencode"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def find_binary(env: dict[str, str] | None = None) -> str | None:
    env = env if env is not None else dict(os.environ)
    override = env.get("ORKESTR_OPENCODE_BIN")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    found = shutil.which("opencode", path=env.get("PATH"))
    if found:
        return found
    home = Path(env.get("HOME") or Path.home())
    for cand in (home / ".opencode" / "bin" / "opencode", home / ".local" / "bin" / "opencode",
                 home / ".npm-global" / "bin" / "opencode", Path("/usr/local/bin/opencode"),
                 Path("/opt/homebrew/bin/opencode")):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _truncate(text: Any, limit: int = OUTPUT_LIMIT) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "\n… [обрезано]"


def sanitize_name(name: str) -> str:
    """OpenCode builds MCP tool names as ``<server>_<tool>`` with ``[^\\w-]`` -> ``_``."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas (string aware)."""
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == "/" and text[i + 1:i + 2] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif c == "/" and text[i + 1:i + 2] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 1
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _load_jsonc(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def user_config_files(cwd: str, env: dict[str, str]) -> list[Path]:
    """Config files OpenCode would read (global, OPENCODE_CONFIG, project chain)."""
    files: list[Path] = []
    xdg = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or Path.home()) / ".config")
    gdir = Path(xdg) / "opencode"
    for name in ("config.json", "opencode.json", "opencode.jsonc"):
        files.append(gdir / name)
    if env.get("OPENCODE_CONFIG"):
        files.append(Path(env["OPENCODE_CONFIG"]))
    cur = Path(cwd).resolve()
    for d in [cur, *cur.parents]:
        for name in ("opencode.json", "opencode.jsonc"):
            files.append(d / name)
            files.append(d / ".opencode" / name)
    return [f for f in files if f.is_file()]


def user_defined_providers(cwd: str, env: dict[str, str]) -> set[str]:
    """Provider ids the user's own configuration already defines."""
    ids: set[str] = set()
    for f in user_config_files(cwd, env):
        prov = _load_jsonc(f).get("provider")
        if isinstance(prov, dict):
            ids.update(str(k) for k in prov)
    inline = env.get("OPENCODE_CONFIG_CONTENT")
    if inline:
        try:
            prov = json.loads(strip_jsonc(inline)).get("provider")
            if isinstance(prov, dict):
                ids.update(str(k) for k in prov)
        except (ValueError, AttributeError):
            pass
    return ids


# --------------------------------------------------------------------------
# local providers
# --------------------------------------------------------------------------

def local_provider_id(server: dict[str, Any]) -> str:
    kind = str(server.get("kind") or "openai_compat")
    url = urlparse(str(server.get("base_url") or ""))
    host = (url.hostname or "").lower()
    port = url.port or (443 if url.scheme == "https" else 80)
    is_local = host in _LOCAL_HOSTS
    if kind in _DEFAULT_PORTS and is_local and port == _DEFAULT_PORTS[kind]:
        return kind
    base = {"lmstudio": "lmstudio", "llamacpp": "llamacpp"}.get(kind, "openai-compat")
    return f"{base}-{'' if is_local else _slug(host) + '-'}{port}"


def _openai_base_url(base_url: str) -> str:
    u = base_url.rstrip("/")
    return u if re.search(r"/v\d+$", u) else u + "/v1"


def server_models(server: dict[str, Any]) -> list[dict[str, Any]]:
    """LLM models reported by a probed server (embeddings skipped)."""
    status = server.get("status") or {}
    out = []
    for m in status.get("models") or []:
        mid = m.get("id")
        if not mid or str(m.get("type") or "").lower().startswith("embed"):
            continue
        out.append(m)
    return out


def model_context(m: dict[str, Any]) -> int | None:
    return m.get("context_window") or m.get("max_context_window") or None


def _output_limit(ctx: int | None) -> int:
    if not ctx:
        return 8192
    return max(1024, min(16384, ctx // 4))


def _model_entry(model_id: str, ctx: int | None) -> dict[str, Any]:
    return {
        "name": model_id,
        "tool_call": True,
        "limit": {"context": ctx or 32768, "output": _output_limit(ctx)},
    }


def build_local_providers(local_servers: list[dict[str, Any]], skip_ids: set[str],
                          wanted_model: str | None = None) -> dict[str, Any]:
    """Provider definitions for OpenCode's ``provider`` config key."""
    providers: dict[str, Any] = {}
    for srv in local_servers or []:
        if srv.get("kind") not in LOCAL_KINDS or not srv.get("base_url"):
            continue
        pid = local_provider_id(srv)
        if pid in skip_ids:
            continue
        models = {m["id"]: _model_entry(m["id"], model_context(m)) for m in server_models(srv)}
        providers[pid] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": f"{srv.get('name') or pid} (Оркестр)",
            "options": {"baseURL": _openai_base_url(str(srv["base_url"])), "apiKey": "local"},
            "models": models,
        }
    if wanted_model and "/" in wanted_model:
        pid, mid = wanted_model.split("/", 1)
        if pid in providers and mid not in providers[pid]["models"]:
            providers[pid]["models"][mid] = _model_entry(mid, None)
    return providers


# --------------------------------------------------------------------------
# permissions and config
# --------------------------------------------------------------------------

def worktree_relative(cwd: str) -> str:
    """cwd relative to OpenCode's worktree root (nearest ``.git`` ancestor, else ``/``).

    OpenCode matches ``edit`` patterns against paths relative to that root, and
    treats the whole worktree (not just the cwd) as "inside the project"."""
    real = os.path.realpath(cwd)
    root = "/"
    cur = Path(real)
    for d in [cur, *cur.parents]:
        if (d / ".git").exists():
            root = str(d)
            break
    return os.path.relpath(real, root)


def permission_config(level: str, mcp_server_name: str = "orkestr",
                      cwd: str | None = None) -> dict[str, Any]:
    """OpenCode ``permission`` object for a contract permission level.

    With ``cwd`` the ``edit`` level is narrowed to that folder (inside a git
    repository OpenCode would otherwise allow the whole repository)."""
    mcp_glob = f"{sanitize_name(mcp_server_name)}_*"
    never = {"question": "deny", "plan_enter": "deny", "plan_exit": "deny"}
    if level == "full":
        return {"*": "allow", "external_directory": "allow", "doom_loop": "allow",
                "read": "allow", **never}
    cfg: dict[str, Any] = {
        "*": "deny",
        "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
        "glob": "allow", "grep": "allow", "list": "allow", "todowrite": "allow",
        mcp_glob: "allow",
        "edit": _edit_rule(level, cwd),
        "bash": "deny", "webfetch": "deny", "websearch": "deny", "codesearch": "deny",
        "task": "deny", "skill": "deny",
        "external_directory": "deny", "doom_loop": "deny",
        **never,
    }
    return cfg


def _edit_rule(level: str, cwd: str | None) -> Any:
    if level != "edit":
        return "deny"
    if not cwd:
        return "allow"
    rel = worktree_relative(cwd)
    if rel in (".", ""):
        return "allow"
    return {"*": "deny", f"{rel}/*": "allow"}


def permission_rules(level: str, mcp_server_name: str = "orkestr",
                     cwd: str | None = None) -> list[dict[str, str]]:
    """Flat ruleset (session level) equivalent to :func:`permission_config`."""
    rules: list[dict[str, str]] = []
    for perm, val in permission_config(level, mcp_server_name, cwd).items():
        if isinstance(val, dict):
            rules.extend({"permission": perm, "pattern": pat, "action": act}
                         for pat, act in val.items())
        else:
            rules.append({"permission": perm, "pattern": "*", "action": val})
    return rules


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def build_config(spec: RunSpec, user_providers: set[str] | None = None,
                 base: dict[str, Any] | None = None) -> dict[str, Any]:
    """The inline config (``OPENCODE_CONFIG_CONTENT``) for one run."""
    cfg: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
        "permission": permission_config(spec.permissions, spec.mcp_server_name, spec.cwd),
    }
    if spec.mcp_url:
        timeout_ms = max(1, int(spec.mcp_tool_timeout_s)) * 1000
        cfg["mcp"] = {sanitize_name(spec.mcp_server_name): {
            "type": "remote", "url": spec.mcp_url, "enabled": True, "timeout": timeout_ms}}
        cfg["experimental"] = {"mcp_timeout": timeout_ms}
    providers = build_local_providers(spec.local_servers, user_providers or set(), spec.model)
    if providers:
        cfg["provider"] = providers
    return _deep_merge(base, cfg) if base else cfg


def build_env(spec: RunSpec, config: dict[str, Any], password: str,
              environ: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(environ if environ is not None else os.environ)
    env.update(spec.env or {})
    env["PWD"] = spec.cwd
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
    env["OPENCODE_SERVER_PASSWORD"] = password
    env["OPENCODE_SERVER_USERNAME"] = _SERVER_USER
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    env["OPENCODE_DISABLE_LSP_DOWNLOAD"] = "1"
    env["OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"] = "1"
    env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] = "1"
    return env


# --------------------------------------------------------------------------
# error texts
# --------------------------------------------------------------------------

def describe_error(err: dict[str, Any] | None) -> str:
    """Russian message for an OpenCode message/session error object."""
    if not err:
        return "неизвестная ошибка"
    name = err.get("name") or "UnknownError"
    data = err.get("data") or {}
    msg = str(data.get("message") or "").strip()
    if name == "APIError":
        low = (msg + " " + str(data.get("responseBody") or "")).lower()
        if "econnrefused" in low or "unable to connect" in low or "connection refused" in low \
                or "fetch failed" in low or "cannot connect" in low:
            return ("не удаётся подключиться к серверу модели (он выключен или адрес неверный): "
                    + msg)
        code = data.get("statusCode")
        return f"ошибка API модели{f' (HTTP {code})' if code else ''}: {msg}"
    if name == "ProviderAuthError":
        return (f"нет доступа к провайдеру {data.get('providerID', '')}: {msg} "
                "Выполните в терминале: opencode auth login").strip()
    if name == "ContextOverflowError":
        return "контекст модели переполнен: " + msg
    if name == "MessageOutputLengthError":
        return "ответ модели оборвался из-за лимита длины"
    if name == "MessageAbortedError":
        return "выполнение прервано"
    if name == "ContentFilterError":
        return "ответ заблокирован фильтром содержимого: " + msg
    return msg or name


def describe_retry(message: str) -> str:
    low = message.lower()
    if "econnrefused" in low or "unable to connect" in low or "cannot connect" in low:
        return ("не удаётся подключиться к серверу модели; проверьте, что LM Studio / llama.cpp "
                "запущен и модель загружена. " + message)
    return message


def tail_lines(lines: list[str], n: int = 12) -> str:
    return "\n".join(lines[-n:]).strip()


# --------------------------------------------------------------------------
# event mapping
# --------------------------------------------------------------------------

class EventMapper:
    """Stateful translation of OpenCode SSE events into RuntimeEvents.

    Feed raw event dicts (``{"type": ..., "properties": {...}}``) to
    :meth:`feed`; it returns the RuntimeEvents to emit.  Pure, no I/O.
    """

    def __init__(self, session_id: str, mcp_server_names: list[str] | None = None,
                 context_window: int | None = None, model: str | None = None,
                 model_resolver: Any = None) -> None:
        self.session_id = session_id
        self.context_window = context_window
        self.model = model
        self._resolver = model_resolver          # (model_id) -> context window | None
        self._mcp = sorted((sanitize_name(n) for n in (mcp_server_names or [])),
                           key=len, reverse=True)
        self._roles: dict[str, str] = {}
        self._part_kind: dict[str, str] = {}      # partID -> text|reasoning
        self._streamed: set[str] = set()          # partIDs that got deltas
        self._done_parts: set[str] = set()
        self._started_tools: set[str] = set()
        self._ended_tools: set[str] = set()
        self._held: dict[str, list[dict[str, Any]]] = {}   # messageID -> parts waiting for a role
        self._early_deltas: dict[str, list[str]] = {}
        self.session_sent = False
        self.steps = 0
        self.cost = 0.0
        self.last_text_by_message: dict[str, str] = {}
        self.errors: list[dict[str, Any]] = []
        self.permission_requests: list[str] = []
        self.retry_attempt = 0
        self.retry_message = ""

    # -- helpers -----------------------------------------------------------
    def split_mcp(self, tool: str) -> dict[str, str] | None:
        for server in self._mcp:
            if tool.startswith(server + "_") and len(tool) > len(server) + 1:
                return {"server": server, "tool": tool[len(server) + 1:]}
        return None

    def _session_event(self) -> list[RuntimeEvent]:
        if self.session_sent:
            return []
        self.session_sent = True
        return [RuntimeEvent("session", {"session_id": self.session_id, "model": self.model,
                                         "context_window": self.context_window})]

    # -- main entry --------------------------------------------------------
    def feed(self, event: dict[str, Any]) -> list[RuntimeEvent]:
        etype = event.get("type")
        props = event.get("properties") or {}
        sid = props.get("sessionID") or (props.get("info") or {}).get("sessionID") \
            or (props.get("part") or {}).get("sessionID")
        if etype == "message.updated":
            return self._on_message(props) if sid == self.session_id else []
        if sid != self.session_id:
            return []
        if etype == "message.part.updated":
            return self._on_part(props.get("part") or {})
        if etype == "message.part.delta":
            return self._on_delta(props)
        if etype == "session.error":
            if props.get("error"):
                self.errors.append(props["error"])
            return []
        if etype == "session.status":
            st = props.get("status") or {}
            if st.get("type") == "retry":
                self.retry_attempt = int(st.get("attempt") or 0)
                self.retry_message = str(st.get("message") or "")
                return [RuntimeEvent("log", {"level": "warn", "message":
                        f"OpenCode повторяет запрос к модели (попытка {self.retry_attempt}): "
                        f"{self.retry_message}"})]
            return []
        if etype == "permission.asked":
            self.permission_requests.append(str(props.get("id") or ""))
            return [RuntimeEvent("log", {"level": "warn", "message":
                    f"OpenCode запросил разрешение ({props.get('permission')}): отклонено автоматически"})]
        return []

    def _on_message(self, props: dict[str, Any]) -> list[RuntimeEvent]:
        info = props.get("info") or {}
        mid = info.get("id")
        role = info.get("role")
        if not mid or not role:
            return []
        out: list[RuntimeEvent] = []
        self._roles[mid] = role
        if role == "assistant":
            pid, model_id = info.get("providerID"), info.get("modelID")
            if pid and model_id and not self.model:
                self.model = f"{pid}/{model_id}"
                if self.context_window is None and self._resolver:
                    self.context_window = self._resolver(self.model)
            if not self.session_sent:
                out.extend(self._session_event())
        for part in self._held.pop(mid, []):
            out.extend(self._on_part(part))
        return out

    def _on_delta(self, props: dict[str, Any]) -> list[RuntimeEvent]:
        pid, delta = props.get("partID"), props.get("delta")
        if not pid or not isinstance(delta, str) or props.get("field", "text") != "text":
            return []
        kind = self._part_kind.get(pid)
        if kind is None:
            self._early_deltas.setdefault(pid, []).append(delta)
            return []
        return self._delta_event(pid, kind, delta)

    def _delta_event(self, pid: str, kind: str, delta: str) -> list[RuntimeEvent]:
        if not delta:
            return []
        self._streamed.add(pid)
        return [RuntimeEvent("text" if kind == "text" else "thinking",
                             {"text": delta, "delta": True})]

    def _on_part(self, part: dict[str, Any]) -> list[RuntimeEvent]:
        ptype = part.get("type")
        mid = part.get("messageID")
        role = self._roles.get(mid)
        if role == "user":
            return []
        if role is None and ptype in ("text", "reasoning", "tool", "step-finish", "retry"):
            self._held.setdefault(mid, []).append(part)
            return []
        out: list[RuntimeEvent] = []
        if not self.session_sent:
            out.extend(self._session_event())
        if ptype in ("text", "reasoning"):
            if part.get("synthetic") or part.get("ignored"):
                return out
            pid = part.get("id")
            first = pid not in self._part_kind
            self._part_kind[pid] = ptype
            if first:
                for d in self._early_deltas.pop(pid, []):
                    out.extend(self._delta_event(pid, ptype, d))
            text = part.get("text") or ""
            ended = bool((part.get("time") or {}).get("end"))
            if ptype == "text":
                self.last_text_by_message[mid] = text
            if ended and pid not in self._done_parts:
                self._done_parts.add(pid)
                if pid not in self._streamed and text.strip():
                    out.append(RuntimeEvent("text" if ptype == "text" else "thinking",
                                            {"text": text, "delta": False}))
            return out
        if ptype == "tool":
            out.extend(self._on_tool(part))
        elif ptype == "step-finish":
            out.extend(self._on_step_finish(part))
        elif ptype == "retry":
            err = part.get("error") or {}
            out.append(RuntimeEvent("log", {"level": "warn", "message":
                       f"Повтор запроса к модели (попытка {part.get('attempt')}): "
                       f"{describe_error(err)}"}))
        return out

    def _on_tool(self, part: dict[str, Any]) -> list[RuntimeEvent]:
        call_id = part.get("callID") or part.get("id")
        name = part.get("tool") or "tool"
        state = part.get("state") or {}
        status = state.get("status")
        out: list[RuntimeEvent] = []
        if status in ("running", "completed", "error") and call_id not in self._started_tools:
            self._started_tools.add(call_id)
            inp = dict(state.get("input") or {})
            if "filePath" in inp and "file_path" not in inp:
                inp["file_path"] = inp["filePath"]
            out.append(RuntimeEvent("tool_start", {
                "tool_id": call_id, "name": name, "input": inp, "mcp": self.split_mcp(name)}))
        if status in ("completed", "error") and call_id not in self._ended_tools:
            self._ended_tools.add(call_id)
            if status == "completed":
                output, is_error = state.get("output") or "", False
            else:
                output, is_error = state.get("error") or "", True
            out.append(RuntimeEvent("tool_end", {
                "tool_id": call_id, "output": _truncate(output), "is_error": is_error}))
        return out

    def _on_step_finish(self, part: dict[str, Any]) -> list[RuntimeEvent]:
        pid = part.get("id")
        if pid in self._done_parts:
            return []
        self._done_parts.add(pid)
        tok = part.get("tokens") or {}
        cache = tok.get("cache") or {}
        inp = int(tok.get("input") or 0)
        outp = int(tok.get("output") or 0) + int(tok.get("reasoning") or 0)
        cr, cw = int(cache.get("read") or 0), int(cache.get("write") or 0)
        cost = part.get("cost")
        self.steps += 1
        self.cost += float(cost or 0.0)
        used = inp + outp + cr + cw
        return [RuntimeEvent("usage", {
            "input_tokens": inp, "output_tokens": outp,
            "cache_read_tokens": cr, "cache_write_tokens": cw,
            "context_used": used or None, "context_window": self.context_window,
            "cost_usd": float(cost) if cost is not None else None})]


def parse_sse_line(line: str) -> dict[str, Any] | None:
    if not line.startswith("data:"):
        return None
    try:
        obj = json.loads(line[5:].strip())
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_models_verbose(text: str) -> list[dict[str, Any]]:
    """Parse ``opencode models --verbose``: ``provider/model`` line + JSON block."""
    dec = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    pos, n = 0, len(text)
    while pos < n:
        eol = text.find("\n", pos)
        eol = n if eol < 0 else eol
        line = text[pos:eol].strip()
        pos = eol + 1
        if not line or line.startswith("{") or "/" not in line or " " in line:
            continue
        j = pos
        while j < n and text[j] in " \r\n\t":
            j += 1
        if j < n and text[j] == "{":
            try:
                obj, end = dec.raw_decode(text, j)
            except ValueError:
                continue
            if isinstance(obj, dict):
                obj["_ref"] = line
                out.append(obj)
            pos = end
        else:
            out.append({"_ref": line})
    return out


def _is_local_url(url: str | None) -> bool:
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if host in _LOCAL_HOSTS or host.endswith(".local") or host.endswith(".lan"):
        return True
    return bool(re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host))


# --------------------------------------------------------------------------
# the adapter
# --------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _terminate_group(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    except Exception:  # noqa: BLE001
        pass
    # the leader may be gone while children survive: always finish the group
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), 2.0)
    except Exception:  # noqa: BLE001
        pass


class OpenCodeAdapter(RuntimeAdapter):
    id = "opencode"
    label = "OpenCode"

    def __init__(self, binary: str | None = None, startup_timeout_s: float = 60.0,
                 capture_path: str | None = None) -> None:
        self._binary = binary
        self.startup_timeout_s = startup_timeout_s
        self.capture_path = capture_path     # debugging: append raw SSE events (JSON lines)

    # -- detection ---------------------------------------------------------
    def _bin(self) -> str | None:
        if self._binary and os.path.isfile(self._binary):
            return self._binary
        return find_binary()

    async def _exec(self, args: list[str], timeout: float, env: dict[str, str] | None = None,
                    cwd: str | None = None) -> tuple[int, str, str]:
        binary = self._bin()
        assert binary
        e = dict(env if env is not None else os.environ)
        e.setdefault("OPENCODE_DISABLE_AUTOUPDATE", "1")
        e["OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"] = "1"
        proc = await asyncio.create_subprocess_exec(
            binary, *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=e, cwd=cwd, start_new_session=True)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:
            await _terminate_group(proc, 1.0)
            raise
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def detect(self, local_servers: list[dict[str, Any]] | None = None) -> RuntimeInfo:
        try:
            return await self._detect(local_servers or [])
        except Exception as exc:  # noqa: BLE001 - detect never raises
            log.warning("opencode detect failed: %s", exc)
            return RuntimeInfo(self.id, self.label, False, error=f"Не удалось проверить OpenCode: {exc}")

    async def _detect(self, local_servers: list[dict[str, Any]]) -> RuntimeInfo:
        binary = self._bin()
        if not binary:
            return RuntimeInfo(self.id, self.label, False, error=NOT_INSTALLED_MSG)
        try:
            code, out, err = await self._exec(["--version"], 5.0)
        except asyncio.TimeoutError:
            return RuntimeInfo(self.id, self.label, False, path=binary,
                               error="OpenCode не ответил на запрос версии за 5 секунд")
        version = out.strip().splitlines()[-1].strip() if out.strip() else None
        if code != 0 or not version:
            return RuntimeInfo(self.id, self.label, False, path=binary,
                               error="OpenCode найден, но не запускается: " + (err.strip()[:300] or f"код {code}"))
        models: list[ModelInfo] = []
        local_ids = {local_provider_id(s) for s in local_servers if s.get("kind") in LOCAL_KINDS}
        try:
            code, out, _ = await self._exec(["models", "--verbose"], 8.0)
            if code == 0:
                for item in parse_models_verbose(out):
                    ref = item["_ref"]
                    prov, _, mid = ref.partition("/")
                    if not mid or prov in local_ids or prov in ("lmstudio", "llamacpp"):
                        continue   # local servers are listed from live data below
                    limit = item.get("limit") or {}
                    api = item.get("api") or {}
                    models.append(ModelInfo(
                        id=ref, label=str(item.get("name") or mid), provider=prov,
                        location="local" if _is_local_url(api.get("url")) else "cloud",
                        context_window=limit.get("context") or None))
        except (asyncio.TimeoutError, OSError) as exc:
            log.info("opencode models failed: %s", exc)
        seen: set[str] = set()
        for srv in local_servers:
            if srv.get("kind") not in LOCAL_KINDS:
                continue
            pid = local_provider_id(srv)
            for m in server_models(srv):
                mid = f"{pid}/{m['id']}"
                if mid in seen:
                    continue
                seen.add(mid)
                models.append(ModelInfo(id=mid, label=str(m["id"]), provider=pid,
                                        location="local", context_window=model_context(m)))
        return RuntimeInfo(self.id, self.label, True, version=version, path=binary, models=models)

    # -- run ---------------------------------------------------------------
    async def run(self, spec: RunSpec) -> AsyncIterator[RuntimeEvent]:  # type: ignore[override]
        binary = self._bin()
        if not binary:
            yield RuntimeEvent("error", {"message": NOT_INSTALLED_MSG, "fatal": True})
            return
        if not os.path.isdir(spec.cwd):
            yield RuntimeEvent("error", {"message": f"Папка проекта не найдена: {spec.cwd}", "fatal": True})
            return

        env0 = dict(os.environ)
        env0.update(spec.env or {})
        base_cfg: dict[str, Any] | None = None
        if env0.get("OPENCODE_CONFIG_CONTENT"):
            try:
                parsed = json.loads(strip_jsonc(env0["OPENCODE_CONFIG_CONTENT"]))
                base_cfg = parsed if isinstance(parsed, dict) else None
            except ValueError:
                base_cfg = None
        user_providers = user_defined_providers(spec.cwd, env0)
        config = build_config(spec, user_providers, base_cfg)
        password = secrets.token_urlsafe(24)
        env = build_env(spec, config, password, env0)
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"

        stderr_tail: list[str] = []
        proc: asyncio.subprocess.Process | None = None
        tasks: list[asyncio.Task[Any]] = []
        client: httpx.AsyncClient | None = None
        sid: str | None = None
        params = {"directory": spec.cwd}
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "serve", "--hostname", "127.0.0.1", "--port", str(port),
                cwd=spec.cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)

            async def pump(stream: asyncio.StreamReader | None) -> None:
                if stream is None:
                    return
                while True:
                    line = await stream.readline()
                    if not line:
                        return
                    text = line.decode("utf-8", "replace").rstrip()
                    if text:
                        stderr_tail.append(_scrub(text, password))
                        del stderr_tail[:-60]

            tasks.append(asyncio.create_task(pump(proc.stdout)))
            tasks.append(asyncio.create_task(pump(proc.stderr)))
            client = httpx.AsyncClient(base_url=base_url, auth=(_SERVER_USER, password),
                                       timeout=httpx.Timeout(30.0, read=None))

            err = await self._wait_ready(client, proc, stderr_tail)
            if err:
                yield RuntimeEvent("error", {"message": err, "fatal": True})
                return

            # model + MCP validation -------------------------------------
            providers = await self._providers(client, params)
            ctx_resolver = lambda m: _lookup_context(providers, m)  # noqa: E731
            model_ctx: int | None = None
            if spec.model:
                prov, _, mid = spec.model.partition("/")
                pinfo = providers.get(prov)
                if not mid or pinfo is None or mid not in pinfo:
                    avail = ", ".join(sorted(f"{p}/{m}" for p, ms in providers.items() for m in ms)[:12])
                    yield RuntimeEvent("error", {"message": (
                        f"Модель «{spec.model}» не найдена в OpenCode. Выберите модель из списка "
                        "(формат провайдер/модель)." + (f" Доступны: {avail}" if avail else "")),
                        "fatal": True})
                    return
                model_ctx = ctx_resolver(spec.model)
            mcp_names = [spec.mcp_server_name] if spec.mcp_url else []
            if spec.mcp_url:
                bad = await self._mcp_problem(client, params, sanitize_name(spec.mcp_server_name))
                if bad:
                    yield RuntimeEvent("error", {"message": (
                        "Не удалось подключить инструменты делегирования Оркестра к OpenCode: " + bad),
                        "fatal": True})
                    return

            # session ------------------------------------------------------
            body: dict[str, Any] = {"title": f"Оркестр: {spec.agent.get('name', '')}"[:80],
                                    "permission": permission_rules(spec.permissions, spec.mcp_server_name,
                                                                   spec.cwd)}
            r = await client.post("/session", params=params, json=body)
            if r.status_code >= 300:
                yield RuntimeEvent("error", {"message": f"OpenCode не создал сессию (HTTP {r.status_code}): "
                                             f"{_short(r.text)}", "fatal": True})
                return
            sid = r.json()["id"]
            mapper = EventMapper(sid, mcp_names, model_ctx, spec.model, ctx_resolver)
            if spec.model:
                for ev in mapper._session_event():
                    yield ev

            queue: asyncio.Queue[Any] = asyncio.Queue()
            connected = asyncio.Event()

            async def sse() -> None:
                try:
                    async with client.stream("GET", "/event", params=params,
                                             timeout=httpx.Timeout(30.0, read=None)) as resp:
                        async for line in resp.aiter_lines():
                            ev = parse_sse_line(line)
                            if ev is None:
                                continue
                            if ev.get("type") == "server.connected":
                                connected.set()
                                continue
                            queue.put_nowait(ev)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    queue.put_nowait({"type": "_sse_closed", "error": str(exc)})
                else:
                    queue.put_nowait({"type": "_sse_closed"})

            sse_task = asyncio.create_task(sse())
            tasks.append(sse_task)
            try:
                await asyncio.wait_for(connected.wait(), 15.0)
            except asyncio.TimeoutError:
                yield RuntimeEvent("error", {"message": "OpenCode не открыл поток событий", "fatal": True})
                return

            msg: dict[str, Any] = {"parts": [{"type": "text", "text": spec.task}]}
            if spec.system_prompt.strip():
                msg["system"] = spec.system_prompt
            if spec.model:
                prov, _, mid = spec.model.partition("/")
                msg["model"] = {"providerID": prov, "modelID": mid}
            post_task = asyncio.create_task(client.post(
                f"/session/{sid}/message", params=params, json=msg,
                timeout=httpx.Timeout(30.0, read=None)))
            tasks.append(post_task)

            idle_deadline: float | None = None
            loop = asyncio.get_running_loop()
            while True:
                if post_task.done() and idle_deadline is None:
                    idle_deadline = loop.time() + 1.5      # drain trailing SSE events
                timeout = 0.25 if idle_deadline is None else max(0.0, idle_deadline - loop.time())
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout if timeout > 0 else 0.001)
                except asyncio.TimeoutError:
                    if idle_deadline is not None and loop.time() >= idle_deadline:
                        break
                    if proc.returncode is not None and not post_task.done():
                        break
                    continue
                if ev.get("type") == "_sse_closed":
                    if post_task.done():
                        break
                    if proc.returncode is not None:
                        break
                    continue
                if ev.get("type") == "permission.asked":
                    props = ev.get("properties") or {}
                    if props.get("sessionID") == sid and props.get("id"):
                        asyncio.create_task(_reject_permission(client, params, props["id"]))
                if self.capture_path:
                    _capture(self.capture_path, ev)
                for out in mapper.feed(ev):
                    yield out
                if mapper.retry_attempt >= MAX_API_RETRIES and not post_task.done():
                    yield RuntimeEvent("error", {"message": (
                        f"Модель не отвечает (OpenCode сделал {mapper.retry_attempt} попытки): "
                        + describe_retry(mapper.retry_message)), "fatal": True})
                    return
                if ev.get("type") == "session.status" and post_task.done():
                    st = ((ev.get("properties") or {}).get("status") or {}).get("type")
                    if st == "idle":
                        break

            # completion ----------------------------------------------------
            if not post_task.done():
                await asyncio.wait({post_task}, timeout=2.0)
            if not post_task.done() or post_task.exception() is not None:
                exc = post_task.exception() if post_task.done() else None
                tail = tail_lines(stderr_tail)
                yield RuntimeEvent("error", {"message": (
                    "OpenCode завершился неожиданно" + (f": {exc}" if exc else "")
                    + (f"\n{tail}" if tail else "")), "fatal": True})
                return
            resp = post_task.result()
            if resp.status_code >= 300:
                tail = tail_lines(stderr_tail)
                yield RuntimeEvent("error", {"message": (
                    f"OpenCode отклонил запрос (HTTP {resp.status_code}): {_short(resp.text)}"
                    + (f"\n{tail}" if tail else "")), "fatal": True})
                return
            data = resp.json()
            info = data.get("info") or {}
            err_obj = info.get("error") or (mapper.errors[-1] if mapper.errors else None)
            if err_obj:
                if err_obj.get("name") == "MessageAbortedError":
                    yield RuntimeEvent("error", {"message": "Выполнение прервано", "fatal": True})
                    return
                tail = tail_lines(stderr_tail, 6)
                yield RuntimeEvent("error", {"message": (
                    "OpenCode завершил работу с ошибкой: " + describe_error(err_obj)
                    + (f"\n{tail}" if tail else "")), "fatal": True})
                return
            text = "".join(p.get("text", "") for p in data.get("parts") or []
                           if p.get("type") == "text" and not p.get("synthetic"))
            yield RuntimeEvent("result", {
                "text": text.strip(), "is_error": False,
                "cost_usd": mapper.cost, "num_turns": mapper.steps or None})
        finally:
            await self._cleanup(client, sid, params, proc, tasks)

    async def _cleanup(self, client: httpx.AsyncClient | None, sid: str | None,
                       params: dict[str, str], proc: asyncio.subprocess.Process | None,
                       tasks: list[asyncio.Task[Any]]) -> None:
        for t in tasks:
            t.cancel()
        if client is not None and sid and proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(client.post(f"/session/{sid}/abort", params=params), 1.0)
            except BaseException:  # noqa: BLE001 - best effort, even when cancelled
                pass
        if proc is not None:
            await _terminate_group(proc)
        if client is not None:
            try:
                await asyncio.wait_for(client.aclose(), 1.0)
            except BaseException:  # noqa: BLE001
                pass
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_ready(self, client: httpx.AsyncClient, proc: asyncio.subprocess.Process,
                          stderr_tail: list[str]) -> str | None:
        deadline = asyncio.get_running_loop().time() + self.startup_timeout_s
        while True:
            if proc.returncode is not None:
                await asyncio.sleep(0.1)
                tail = tail_lines(stderr_tail)
                return (f"OpenCode завершился при запуске (код {proc.returncode})"
                        + (f":\n{tail}" if tail else ""))
            try:
                r = await client.get("/global/health", timeout=2.0)
                if r.status_code == 200:
                    return None
            except httpx.HTTPError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                tail = tail_lines(stderr_tail)
                return (f"OpenCode не запустился за {int(self.startup_timeout_s)} с"
                        + (f":\n{tail}" if tail else ""))
            await asyncio.sleep(0.15)

    async def _providers(self, client: httpx.AsyncClient, params: dict[str, str]) -> dict[str, dict[str, Any]]:
        """provider id -> {model id -> model info} as the running server sees them."""
        try:
            r = await client.get("/config/providers", params=params, timeout=20.0)
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for p in data.get("providers") or []:
            models = p.get("models") or {}
            if isinstance(models, list):
                models = {m.get("id"): m for m in models}
            out[p.get("id")] = models
        return out

    async def _mcp_problem(self, client: httpx.AsyncClient, params: dict[str, str],
                           name: str) -> str | None:
        """None when the MCP server is connected, else a short reason."""
        for _ in range(20):
            try:
                r = await client.get("/mcp", params=params, timeout=15.0)
                st = (r.json() or {}).get(name) or {}
            except (httpx.HTTPError, ValueError):
                return None   # cannot tell; do not block the run
            status = st.get("status")
            if status == "connected":
                return None
            if status in ("failed", "needs_auth", "needs_client_registration", "disabled"):
                return f"{status}: {st.get('error', '')}".strip(": ")
            await asyncio.sleep(0.25)
        return "таймаут подключения"


def _lookup_context(providers: dict[str, dict[str, Any]], model: str) -> int | None:
    prov, _, mid = model.partition("/")
    info = (providers.get(prov) or {}).get(mid) or {}
    ctx = (info.get("limit") or {}).get("context")
    return int(ctx) if ctx else None


async def _reject_permission(client: httpx.AsyncClient, params: dict[str, str], req_id: str) -> None:
    try:
        await client.post(f"/permission/{req_id}/reply", params=params,
                          json={"reply": "reject", "message": "Запрещено настройками Оркестра"},
                          timeout=5.0)
    except Exception:  # noqa: BLE001
        pass


def _capture(path: str, obj: Any) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _short(text: str, n: int = 300) -> str:
    return text.strip().replace("\n", " ")[:n]


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text
