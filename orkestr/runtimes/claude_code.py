"""Claude Code runtime adapter (headless CLI -> RuntimeEvent).

Everything below was established against the real CLI (`claude` 2.1.323 at
/opt/node22/bin/claude, logged in) on 2026-11-13 by `--help`, by strings in the
binary (model alias table, model context windows) and by tiny real runs with
`--model haiku`. The raw captures are in tests/fixtures/claude/. The online docs
were NOT fetched (no web access was used); anything not covered by an
experiment is listed under "Unverified" at the end.

Invocation (one process per run, prompt on STDIN)
-------------------------------------------------
    claude -p --output-format stream-json --verbose --include-partial-messages
           --session-id <uuid4> --permission-prompts none
           [--model M] [--append-system-prompt TEXT]
           <permission flags> [--mcp-config FILE --allowedTools mcp__<server>]

* `--output-format stream-json` REQUIRES `--verbose` with `-p` (the CLI exits
  with "requires --verbose" otherwise).
* `--include-partial-messages` is needed for token level deltas: it adds
  `stream_event` lines (Anthropic SSE events). Without it a message arrives
  only as complete `assistant` lines.
* The prompt goes through stdin, not argv: `--tools`, `--allowedTools` and
  `--mcp-config` are variadic and would swallow a positional prompt, and a
  prompt that starts with "-" would be parsed as a flag.
* `--model` takes an alias (opus, sonnet, haiku, fable, best, opus[1m],
  sonnet[1m], fable[1m], opusplan; table read from the binary) or a full id.
* `--append-system-prompt TEXT` appends to the default system prompt.
* `--session-id <uuid>` presets the session id (must be a UUID). The transcript
  is then ~/.claude/projects/<cwd with / -> ->/<uuid>.jsonl. The id is
  registered in OWN_SESSION_IDS so the session watcher skips our own runs.
* `--mcp-config FILE` with {"mcpServers": {name: {"type": "http", "url": u}}}
  (temp file, mode 0600, deleted afterwards; the URL holds the run token).
  `--strict-mcp-config` is NOT used: the user's own MCP servers stay available
  (in `read`/`edit` their tools still need approval, which nobody can give, so
  they are denied; in `full` they work).
* `--permission-prompts none` (2.1.x): "nobody answers prompts, anything that
  would prompt is denied automatically". Verified: the model gets a tool_result
  with is_error=true ("Permission for this tool use was denied ...") and the
  run continues; nothing hangs. The adapter checks `claude --help` once and
  omits the flag when the CLI does not know it (older CLIs deny by default
  in -p mode as well).

Permission mapping (all verified by experiment in a temp dir)
-------------------------------------------------------------
* read : `--tools Read,Grep,Glob,Agent`. `--tools` removes every other
         built-in (Edit, Write, Bash, WebFetch, ...) for the main agent and for
         sub-agents ("Bash is disabled for this session, in subagents as
         well"). Reads outside cwd are denied (default mode auto-accepts reads
         only inside the working directories; do NOT add `--allowedTools Read`,
         that would allow Read everywhere - I saw /etc/hostname read that way).
* edit : `--tools Read,Grep,Glob,Agent,Edit,Write,NotebookEdit
         --permission-mode acceptEdits`. Write inside cwd is accepted, Write
         outside cwd is denied (verified), no shell, no web.
* full : `--permission-mode bypassPermissions` and the default tool set. Works
         as root in this sandbox.
* MCP  : with spec.mcp_url the config is added and `--allowedTools
         mcp__<server>` allows every tool of that server without a prompt, in
         all modes (without it the call is denied - verified). `--tools` does
         not hide MCP tools (verified).

MCP tool timeout
----------------
Env var `MCP_TOOL_TIMEOUT` (milliseconds) is set to spec.mcp_tool_timeout_s *
1000. Verified: with 2000 a 5 s tool call fails with "MCP server "x" tool "slow"
timed out after 2s" (is_error tool_result); with 900000 a 5 s and a 70 s call
succeed.

Environment hygiene
-------------------
The subprocess gets os.environ minus markers of an enclosing Claude Code /
Claude-hosted session, plus spec.env. Stripped (see `_STRIP_*`): CLAUDECODE,
AI_AGENT, CLAUDE_PID, SESSION_INGRESS_URL and CLAUDE_CODE_{ENTRYPOINT,EXECPATH,
SSE_PORT,SESSION*,REMOTE*,CCR*,USE_CCR*,MESSAGING*,CHILD*,CONTAINER*,WORKER*,
HOSTED*,TEE*,POST_FOR*,SYNC*,ARTIFACT*,INCLUDE_PARTIAL*,...}, CLAUDE_SESSION_INGRESS*,
CLAUDE_EFFORT, CLAUDE_AUTOCOMPACT_PCT_OVERRIDE and friends. Why: when Orkestr is
started from inside a Claude Code session the nested CLI otherwise believes it
is that session's child - e.g. CLAUDE_CODE_USE_CCR_V2 / SESSION_INGRESS_* would
make it report its events to the OUTER session, and CLAUDE_CODE_ENTRYPOINT /
CLAUDE_CODE_INCLUDE_PARTIAL_MESSAGES change the stream (extra `active_goal` /
`autocompact_state` lines were observed, plugins/tools of the outer host
appeared). Kept: ANTHROPIC_*, CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CONFIG_DIR,
CLAUDE_CODE_USE_BEDROCK/VERTEX/..., proxies, PATH, HOME. Authentication still
worked with the stripped environment in the sandbox.

Stream schema (one JSON object per line) and mapping
----------------------------------------------------
* {"type":"system","subtype":"init", session_id, model, tools, mcp_servers:
  [{name,status}], cwd, ...}                    -> `session`; a `log` warn for
  every MCP server whose status is not "connected".
* system/status, thinking_tokens, permission_denied, task_started/progress/
  updated/notification (sub-agent bookkeeping), post_turn_summary,
  rate_limit_event, active_goal, autocompact_state -> ignored (rate limit
  rejection -> `log` warn).
* {"type":"stream_event","event":{...}, "parent_tool_use_id", "api_message_id"}
  message_start (usage, model, id) / content_block_start / content_block_delta
  (text_delta, thinking_delta, input_json_delta, signature_delta) /
  content_block_stop / message_delta (FINAL usage of the message) /
  message_stop.  text_delta -> text(delta=True); thinking_delta with non-empty
  text -> thinking(delta=True). In this CLI version thinking text is redacted
  (empty string + signature), so no thinking events appear for it.
* {"type":"assistant","message":{id,model,content:[block],usage,...},
  "parent_tool_use_id", "timestamp"}: ONE LINE PER CONTENT BLOCK, all lines of
  one API message share message.id and usage. Blocks: text -> text(delta=False)
  (skipped when the same message already streamed deltas), thinking (non-empty)
  -> thinking, tool_use -> tool_start (name, input, mcp split from
  "mcp__server__tool"), server_tool_use likewise.
* {"type":"user","message":{content:[{type:"tool_result",tool_use_id,content,
  is_error}]}, "parent_tool_use_id"}             -> tool_end (content is a
  string or a list of {type:text|image} blocks, truncated to 20 000 chars).
* {"type":"result", subtype, is_error, result, total_cost_usd, num_turns, usage,
  modelUsage:{model:{contextWindow,...}}, permission_denials}
  -> final `usage` with the CLI's own context window (zero token deltas) and
  `result`.

Usage rules
-----------
One `usage` event per API message id (never per content-block line). With
partial messages the assistant lines carry the output_tokens of message_start
(e.g. 5 where the truth is 10), so the event is emitted at `message_delta`,
which has the final numbers; for messages that were not streamed (sub-agent
messages, transcripts) it is emitted at the first assistant line. If the run
ends before a message_delta arrives, the last known assistant-line usage is
flushed at `result`. context_used = input + cache_read + cache_creation +
output of the latest MAIN-thread turn. Sub-agent turns (parent_tool_use_id set)
contribute tokens but context_used/context_window are None for them, so the
main gauge is not polluted. cost_usd is None per turn; `result.cost_usd`
(total_cost_usd, which already includes sub-agents) is authoritative.
Context window: result.modelUsage[model].contextWindow when present (real value
seen: 1 000 000 for claude-haiku-5-5), else `guess_context_window()` (table from
the binary: 4.x models and older 200k, 5.x and opus >= 4.7 and `[1m]` 1M,
unknown 200k) and never smaller than context_used.

Sub-agents (tool "Agent"; older CLIs "Task")
--------------------------------------------
Their assistant/user lines are in the same stdout stream with
parent_tool_use_id = id of the Agent tool_use (no stream_events for them).
They are passed through as tool_start/tool_end with an extra
`data["parent_tool_use_id"]` (additive field), their usage is counted once;
their prose/thinking is dropped (it is not the parent's message).
Observed trap: the model may start the sub-agent with run_in_background=true;
then the CLI prints a first `result` (+ a second `init`), keeps running and
prints another `result` when the background task wakes the main loop. Two
defences: env CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 (verified: one foreground
sub-agent, one result) and the adapter yields only the LAST `result` before
the process exits (fixture subagent_background.jsonl); `session` only once.

Failure handling
----------------
Not installed / not logged in -> `error(fatal)` in Russian with the command to
run. A result with is_error whose text looks like an auth failure is turned
into the same fatal error. Other is_error results are yielded as
result(is_error=True). Exit without a result -> fatal error with the last
stderr lines. Cancellation / generator close -> SIGTERM to the process group,
SIGKILL after 3 s.

Unverified
----------
* The exact CLI output when logged out (cannot log out in the sandbox; the
  detection works on message text patterns and on `claude auth status` JSON).
* `--permission-prompts` absence fallback on an old CLI.
* Install hints in the "not installed" message come from general knowledge.
* Alias list comes from the binary, only `haiku` was run.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from .base import ModelInfo, RunSpec, RuntimeAdapter, RuntimeEvent, RuntimeInfo

log = logging.getLogger("orkestr.claude")

#: Session ids launched by the adapter; the session watcher skips them.
OWN_SESSION_IDS: set[str] = set()

MAX_OUTPUT_CHARS = 20_000
KILL_GRACE_S = 3.0
STDERR_TAIL_LINES = 12
DEFAULT_CONTEXT_WINDOW = 200_000
ONE_M = 1_000_000

INSTALL_HINT = (
    "Claude Code не найден. Установите его: curl -fsSL https://claude.ai/install.sh | bash "
    "(или npm install -g @anthropic-ai/claude-code), затем выполните в терминале: claude auth login"
)
LOGIN_HINT = "Claude Code не авторизован. Выполните в терминале: claude auth login"

# --------------------------------------------------------------------------- env

_STRIP_EXACT = frozenset({
    "CLAUDECODE", "AI_AGENT", "CLAUDE_PID", "SESSION_INGRESS_URL",
    "CLAUDE_AFTER_LAST_COMPACT", "CLAUDE_AUTO_BACKGROUND_TASKS",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_ADDITIONAL_DIRECTORIES",
    "CLAUDE_EFFORT", "CLAUDE_ENABLE_STREAM_WATCHDOG",
})
_STRIP_PREFIXES = (
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_SESSION", "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_CCR", "CLAUDE_CODE_USE_CCR",
    "CLAUDE_CODE_MESSAGING", "CLAUDE_CODE_CHILD", "CLAUDE_CODE_CONTAINER",
    "CLAUDE_CODE_WORKER", "CLAUDE_CODE_HOSTED", "CLAUDE_CODE_TEE", "CLAUDE_CODE_POST_FOR",
    "CLAUDE_CODE_SYNC", "CLAUDE_CODE_ARTIFACT", "CLAUDE_CODE_INCLUDE_PARTIAL",
    "CLAUDE_CODE_DIAGNOSTICS", "CLAUDE_CODE_DEBUG", "CLAUDE_CODE_ACCOUNT",
    "CLAUDE_CODE_ORGANIZATION", "CLAUDE_CODE_USER_EMAIL", "CLAUDE_CODE_VERSION",
    "CLAUDE_CODE_PROVIDER_MANAGED", "CLAUDE_CODE_PROXY_RES", "CLAUDE_CODE_MCP_",
    "CLAUDE_CODE_JIT", "CLAUDE_CODE_HOLD", "CLAUDE_CODE_GZIP", "CLAUDE_CODE_ENVIRONMENT",
    "CLAUDE_CODE_ELEGANT", "CLAUDE_CODE_DISABLE_BUILTIN", "CLAUDE_CODE_DISABLE_TERMINAL",
    "CLAUDE_CODE_CONFIG_WATCH", "CLAUDE_CODE_BG", "CLAUDE_CODE_ADDITIONAL",
    "CLAUDE_CODE_MAX_SUBAGENT", "CLAUDE_SESSION_INGRESS",
)


def build_env(
    extra: dict[str, str] | None = None,
    *,
    mcp_tool_timeout_s: int | None = None,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment for the CLI: inherited minus nested-session markers, plus extras."""
    src = os.environ if base is None else base
    env = {
        k: v for k, v in src.items()
        if k not in _STRIP_EXACT and not k.startswith(_STRIP_PREFIXES)
    }
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    if mcp_tool_timeout_s:
        env["MCP_TOOL_TIMEOUT"] = str(int(mcp_tool_timeout_s) * 1000)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


# ------------------------------------------------------------------ permissions

READ_TOOLS = ("Read", "Grep", "Glob", "Agent")
EDIT_TOOLS = READ_TOOLS + ("Edit", "Write", "NotebookEdit")


def permission_args(permissions: str) -> list[str]:
    if permissions == "full":
        return ["--permission-mode", "bypassPermissions"]
    if permissions == "edit":
        return ["--tools", ",".join(EDIT_TOOLS), "--permission-mode", "acceptEdits"]
    # "read" and anything unknown fall back to the safest mode
    return ["--tools", ",".join(READ_TOOLS)]


def build_command(
    claude_path: str,
    spec: RunSpec,
    session_id: str,
    mcp_config_path: str | None,
    *,
    permission_prompts_flag: bool = True,
) -> list[str]:
    cmd = [
        claude_path, "-p",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--session-id", session_id,
    ]
    if permission_prompts_flag:
        cmd += ["--permission-prompts", "none"]
    if spec.model:
        cmd += ["--model", spec.model]
    if spec.system_prompt and spec.system_prompt.strip():
        cmd += ["--append-system-prompt", spec.system_prompt]
    cmd += permission_args(spec.permissions)
    if mcp_config_path:
        cmd += ["--mcp-config", mcp_config_path, "--allowedTools", f"mcp__{spec.mcp_server_name}"]
    return cmd


def mcp_config_json(spec: RunSpec) -> str:
    return json.dumps({"mcpServers": {spec.mcp_server_name: {"type": "http", "url": spec.mcp_url}}})


# ----------------------------------------------------------------- model table

_MODEL_RE = re.compile(r"claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2})(?!\d))?")
_ALIASES = ("opus", "sonnet", "haiku", "fable", "best", "opusplan")


def guess_context_window(model: str | None) -> int:
    """Context window by model id/alias when the CLI has not told us (yet)."""
    if not model:
        return DEFAULT_CONTEXT_WINDOW
    m = model.strip().lower()
    if m.endswith("[1m]"):
        return ONE_M
    if m in _ALIASES:
        return ONE_M  # aliases point at the current generation (5.x, native 1M)
    mm = _MODEL_RE.search(m)
    if mm:
        family, major = mm.group(1), int(mm.group(2))
        minor = int(mm.group(3) or 0)
        if major >= 5:
            return ONE_M
        if family == "opus" and major == 4 and minor >= 7:
            return ONE_M
    return DEFAULT_CONTEXT_WINDOW


MODELS: list[ModelInfo] = [
    ModelInfo("opus", "Opus (последняя)", "anthropic", "cloud", guess_context_window("opus")),
    ModelInfo("sonnet", "Sonnet (последняя)", "anthropic", "cloud", guess_context_window("sonnet")),
    ModelInfo("haiku", "Haiku (последняя)", "anthropic", "cloud", guess_context_window("haiku")),
    ModelInfo("fable", "Fable (последняя)", "anthropic", "cloud", guess_context_window("fable")),
    ModelInfo("opus[1m]", "Opus, контекст 1M", "anthropic", "cloud", ONE_M),
    ModelInfo("sonnet[1m]", "Sonnet, контекст 1M", "anthropic", "cloud", ONE_M),
]


# ------------------------------------------------------------------- utilities

def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… [обрезано]"


def parse_ts(value: Any) -> int | None:
    """ISO-8601 ('2026-11-13T07:13:21.081Z') -> epoch milliseconds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def split_mcp_name(name: str) -> dict[str, str] | None:
    if not name.startswith("mcp__"):
        return None
    server, sep, tool = name[5:].partition("__")
    if not sep or not server or not tool:
        return None
    return {"server": server, "tool": tool}


def tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(str(b.get("text", "")))
                elif b.get("type") == "image":
                    parts.append("[изображение]")
                else:
                    parts.append(json.dumps(b, ensure_ascii=False)[:500])
            else:
                parts.append(str(b))
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


_AUTH_RE = re.compile(
    r"not logged in|please run /login|run /login|invalid api key|authentication_error|"
    r"oauth token has expired|failed to authenticate|credentials? (?:are |is )?(?:missing|invalid)",
    re.IGNORECASE,
)


def looks_like_auth_error(text: str) -> bool:
    return bool(text and _AUTH_RE.search(text))


def user_prompt_text(obj: dict[str, Any]) -> str | None:
    """Text of a real user prompt line (transcript or stream), None for tool results."""
    if obj.get("type") != "user" or obj.get("isMeta"):
        return None
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(t for t in texts if t) or None
    return None


# ------------------------------------------------------------------ translator

class StreamTranslator:
    """Stateful mapping of Claude Code JSON objects to RuntimeEvents.

    Used by the adapter (stdout stream-json) and by the session watcher
    (transcript lines). `feed(obj)` returns the events for one object;
    `finish()` flushes pending usage."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.model: str | None = None
        self.context_window: int | None = None  # from the CLI, authoritative
        self.last_context_used: int | None = None
        self.result_seen = False
        self._streaming: set[str] = set()        # message ids seen via message_start (main thread)
        self._text_streamed: set[str] = set()
        self._thinking_streamed: set[str] = set()
        self._usage_done: set[str] = set()
        self._pending_usage: dict[str, tuple[dict[str, Any], bool]] = {}
        self._tools_started: set[str] = set()
        self._tools_ended: set[str] = set()
        self._cur_msg: str | None = None
        self._session_sent = False

    # -- helpers
    def _window(self, model: str | None, used: int | None) -> int:
        w = self.context_window or guess_context_window(model or self.model)
        if used and used > w:
            w = ONE_M if used <= ONE_M else used
        return w

    def _usage_event(self, usage: dict[str, Any], main: bool, model: str | None) -> RuntimeEvent:
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        cr = int(usage.get("cache_read_input_tokens") or 0)
        cw = int(usage.get("cache_creation_input_tokens") or 0)
        used: int | None = None
        window: int | None = None
        if main:
            used = inp + cr + cw + out
            self.last_context_used = used
            window = self._window(model, used)
        return RuntimeEvent("usage", {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_tokens": cr, "cache_write_tokens": cw,
            "context_used": used, "context_window": window, "cost_usd": None,
        })

    # -- main entry
    def feed(self, obj: dict[str, Any]) -> list[RuntimeEvent]:
        t = obj.get("type")
        try:
            if t == "stream_event":
                return self._stream_event(obj)
            if t == "assistant":
                return self._assistant(obj)
            if t == "user":
                return self._user(obj)
            if t == "system":
                return self._system(obj)
            if t == "result":
                return self._result(obj)
            if t == "rate_limit_event":
                info = obj.get("rate_limit_info") or {}
                status = info.get("status")
                if status and status not in ("allowed", "allowed_warning"):
                    return [RuntimeEvent("log", {"level": "warn", "message": f"Лимит Claude: {status}"})]
        except Exception as exc:  # a malformed line must never kill the run
            log.warning("claude translator failed on a %s line: %r", t, exc)
            return [RuntimeEvent("log", {"level": "warn", "message": f"Не удалось разобрать строку потока ({t})"})]
        return []

    def finish(self) -> list[RuntimeEvent]:
        out: list[RuntimeEvent] = []
        for mid in list(self._pending_usage):
            usage, main = self._pending_usage.pop(mid)
            if mid not in self._usage_done:
                self._usage_done.add(mid)
                out.append(self._usage_event(usage, main, self.model))
        return out

    # -- handlers
    def _system(self, obj: dict[str, Any]) -> list[RuntimeEvent]:
        sub = obj.get("subtype")
        if sub == "init":
            self.session_id = obj.get("session_id") or self.session_id
            self.model = obj.get("model") or self.model
            if self._session_sent:  # the CLI re-inits when a background task wakes the main loop
                return []
            self._session_sent = True
            events = [RuntimeEvent("session", {
                "session_id": self.session_id, "model": self.model,
                "context_window": self.context_window or guess_context_window(self.model),
            })]
            for srv in obj.get("mcp_servers") or []:
                if isinstance(srv, dict) and srv.get("status") not in (None, "connected"):
                    events.append(RuntimeEvent("log", {
                        "level": "warn",
                        "message": f"MCP-сервер {srv.get('name')} не подключился: {srv.get('status')}",
                    }))
            return events
        if sub == "api_retry":
            return [RuntimeEvent("log", {"level": "warn", "message": "Claude API: повтор запроса"})]
        return []

    def _stream_event(self, obj: dict[str, Any]) -> list[RuntimeEvent]:
        if obj.get("parent_tool_use_id"):
            return []
        ev = obj.get("event") or {}
        et = ev.get("type")
        if et == "message_start":
            msg = ev.get("message") or {}
            mid = msg.get("id") or obj.get("api_message_id")
            if mid:
                self._cur_msg = mid
                self._streaming.add(mid)
            self.model = msg.get("model") or self.model
            return []
        mid = obj.get("api_message_id") or self._cur_msg
        if et == "content_block_delta":
            d = ev.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta" and d.get("text"):
                if mid:
                    self._text_streamed.add(mid)
                return [RuntimeEvent("text", {"text": d["text"], "delta": True})]
            if dt == "thinking_delta" and d.get("thinking"):
                if mid:
                    self._thinking_streamed.add(mid)
                return [RuntimeEvent("thinking", {"text": d["thinking"], "delta": True})]
            return []
        if et == "message_delta":
            usage = ev.get("usage")
            if mid and usage and mid not in self._usage_done:
                self._usage_done.add(mid)
                self._pending_usage.pop(mid, None)
                return [self._usage_event(usage, True, self.model)]
        return []

    def _assistant(self, obj: dict[str, Any]) -> list[RuntimeEvent]:
        msg = obj.get("message") or {}
        mid = msg.get("id")
        parent = obj.get("parent_tool_use_id")
        main = not parent
        if main and msg.get("model"):
            self.model = msg["model"]
        events: list[RuntimeEvent] = []
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                text = block.get("text") or ""
                # sub-agent prose is not the parent's message: dropped
                if text and main and mid not in self._text_streamed:
                    events.append(RuntimeEvent("text", {"text": text, "delta": False}))
            elif bt == "thinking":
                text = block.get("thinking") or ""
                if text and main and mid not in self._thinking_streamed:
                    events.append(RuntimeEvent("thinking", {"text": text, "delta": False}))
            elif bt in ("tool_use", "server_tool_use", "mcp_tool_use"):
                tid = block.get("id")
                if not tid or tid in self._tools_started:
                    continue
                self._tools_started.add(tid)
                name = str(block.get("name") or "")
                inp = block.get("input")
                data: dict[str, Any] = {
                    "tool_id": tid, "name": name,
                    "input": inp if isinstance(inp, dict) else {},
                    "mcp": split_mcp_name(name),
                }
                if parent:
                    data["parent_tool_use_id"] = parent
                events.append(RuntimeEvent("tool_start", data))
            elif isinstance(bt, str) and bt.endswith("_tool_result") and bt != "tool_result":
                tid = block.get("tool_use_id")
                if tid and tid not in self._tools_ended:
                    self._tools_ended.add(tid)
                    events.append(RuntimeEvent("tool_end", {
                        "tool_id": tid,
                        "output": truncate(tool_result_text(block.get("content"))),
                        "is_error": False,
                    }))
        usage = msg.get("usage")
        if mid and isinstance(usage, dict) and mid not in self._usage_done:
            if main and mid in self._streaming:
                self._pending_usage[mid] = (usage, True)   # wait for message_delta
            else:
                self._usage_done.add(mid)
                events.append(self._usage_event(usage, main, msg.get("model")))
        return events

    def _user(self, obj: dict[str, Any]) -> list[RuntimeEvent]:
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            return []
        events: list[RuntimeEvent] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            if not tid or tid in self._tools_ended:
                continue
            self._tools_ended.add(tid)
            data: dict[str, Any] = {
                "tool_id": tid,
                "output": truncate(tool_result_text(block.get("content"))),
                "is_error": bool(block.get("is_error")),
            }
            if obj.get("parent_tool_use_id"):
                data["parent_tool_use_id"] = obj["parent_tool_use_id"]
            events.append(RuntimeEvent("tool_end", data))
        return events

    def _result(self, obj: dict[str, Any]) -> list[RuntimeEvent]:
        events = self.finish()
        usage_by_model = obj.get("modelUsage")
        if isinstance(usage_by_model, dict) and usage_by_model:
            entry = usage_by_model.get(self.model or "")
            if not isinstance(entry, dict):
                entries = [e for e in usage_by_model.values() if isinstance(e, dict) and e.get("contextWindow")]
                entry = max(entries, key=lambda e: e.get("contextWindow") or 0) if entries else None
            if isinstance(entry, dict) and entry.get("contextWindow"):
                self.context_window = int(entry["contextWindow"])
        if self.last_context_used is not None:
            events.append(RuntimeEvent("usage", {
                "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                "context_used": self.last_context_used,
                "context_window": self._window(self.model, self.last_context_used),
                "cost_usd": None,
            }))
        text = obj.get("result")
        is_error = bool(obj.get("is_error"))
        if not isinstance(text, str) or not text:
            sub = obj.get("subtype") or ""
            text = "" if not is_error else {
                "error_max_turns": "Достигнут лимит ходов Claude Code.",
                "error_during_execution": "Claude Code завершился с ошибкой во время работы.",
            }.get(sub, f"Claude Code вернул ошибку ({sub or 'без описания'}).")
        cost = obj.get("total_cost_usd")
        events.append(RuntimeEvent("result", {
            "text": text, "is_error": is_error,
            "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
            "num_turns": obj.get("num_turns") if isinstance(obj.get("num_turns"), int) else None,
        }))
        self.result_seen = True
        return events


# ---------------------------------------------------------------------- adapter

def find_claude() -> str | None:
    override = os.environ.get("ORKESTR_CLAUDE_BIN")
    if override and os.access(override, os.X_OK):
        return override
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for cand in (
        home / ".claude" / "local" / "claude",
        home / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/node22/bin/claude"),
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


async def _terminate_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the whole process group, SIGKILL after KILL_GRACE_S."""
    pid = proc.pid
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE_S)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    # the leader may be gone while children still hold the group: always follow up
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), 2.0)


class ClaudeCodeAdapter(RuntimeAdapter):
    id = "claude_code"
    label = "Claude Code"

    def __init__(self, claude_path: str | None = None) -> None:
        self._path_override = claude_path
        self._prompts_flag: bool | None = None

    def _find(self) -> str | None:
        if self._path_override:
            return self._path_override if os.access(self._path_override, os.X_OK) else None
        return find_claude()

    async def _capture(self, path: str, *args: str, timeout: float = 8.0) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            path, *args,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=build_env(), start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            await _terminate_group(proc)
            raise
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def _supports_prompts_flag(self, path: str) -> bool:
        if self._prompts_flag is None:
            try:
                _, out, _ = await self._capture(path, "--help", timeout=10.0)
                self._prompts_flag = "--permission-prompts" in out
            except Exception:
                self._prompts_flag = True
        return self._prompts_flag

    async def detect(self, local_servers: list[dict[str, Any]] | None = None) -> RuntimeInfo:
        path = self._find()
        if not path:
            return RuntimeInfo(self.id, self.label, False, None, None, INSTALL_HINT, [])
        version: str | None = None
        try:
            rc, out, _ = await self._capture(path, "--version", timeout=8.0)
            m = re.search(r"\d+\.\d+\.\d+\S*", out)
            version = m.group(0) if m else (out.strip() or None)
        except Exception as exc:
            return RuntimeInfo(self.id, self.label, False, None, path,
                               f"Не удалось запустить Claude Code ({exc.__class__.__name__}). "
                               "Проверьте установку: claude --version", [])
        error: str | None = None
        available = True
        try:
            _, out, _ = await self._capture(path, "auth", "status", timeout=8.0)
            status = json.loads(out)
            has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
            if isinstance(status, dict) and status.get("loggedIn") is False and not has_key:
                available, error = False, LOGIN_HINT
        except Exception:
            pass  # unknown: do not block the runtime on a failed status probe
        return RuntimeInfo(self.id, self.label, available, version, path, error,
                           [ModelInfo(**m.to_dict()) for m in MODELS] if available else [])

    # ------------------------------------------------------------------ run
    async def run(self, spec: RunSpec) -> AsyncIterator[RuntimeEvent]:  # type: ignore[override]
        path = self._find()
        if not path:
            yield RuntimeEvent("error", {"message": INSTALL_HINT, "fatal": True})
            return
        if not os.path.isdir(spec.cwd):
            yield RuntimeEvent("error", {"message": f"Папка проекта не найдена: {spec.cwd}", "fatal": True})
            return

        session_id = str(uuid.uuid4())
        OWN_SESSION_IDS.add(session_id)
        tmpdir: str | None = None
        cfg_path: str | None = None
        proc: asyncio.subprocess.Process | None = None
        tasks: list[asyncio.Task[Any]] = []
        try:
            if spec.mcp_url:
                tmpdir = tempfile.mkdtemp(prefix="orkestr-claude-")
                cfg_path = os.path.join(tmpdir, "mcp.json")
                fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(mcp_config_json(spec))
            cmd = build_command(path, spec, session_id, cfg_path,
                                permission_prompts_flag=await self._supports_prompts_flag(path))
            env = build_env(spec.env, mcp_tool_timeout_s=spec.mcp_tool_timeout_s)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=spec.cwd, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True,
                    limit=64 * 1024 * 1024,
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                yield RuntimeEvent("error", {
                    "message": f"Не удалось запустить Claude Code: {exc.strerror or exc}. " + INSTALL_HINT,
                    "fatal": True,
                })
                return

            stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
            tasks.append(asyncio.create_task(self._pump_stderr(proc, stderr_tail)))
            tasks.append(asyncio.create_task(self._feed_stdin(proc, spec.task)))

            tr = StreamTranslator()
            assert proc.stdout is not None
            fatal_sent = False
            held_result: RuntimeEvent | None = None
            while True:
                try:
                    raw = await proc.stdout.readline()
                except ValueError:
                    yield RuntimeEvent("log", {"level": "warn", "message": "Слишком длинная строка потока пропущена"})
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    yield RuntimeEvent("log", {"level": "debug", "message": line[:300]})
                    continue
                if not isinstance(obj, dict):
                    continue
                for ev in tr.feed(obj):
                    if ev.type == "result":
                        # a background task may wake the CLI for another turn after a result;
                        # only the last result before exit is the result of the run
                        held_result = ev
                        continue
                    yield ev
            try:
                rc = await asyncio.wait_for(proc.wait(), 10.0)
            except asyncio.TimeoutError:
                rc = None
            with contextlib.suppress(Exception):  # let the stderr pump drain
                await asyncio.wait_for(asyncio.shield(tasks[0]), 2.0)
            for ev in tr.finish():
                yield ev
            if held_result is not None:
                if held_result.data.get("is_error") and looks_like_auth_error(held_result.data.get("text", "")):
                    yield RuntimeEvent("error", {"message": LOGIN_HINT, "fatal": True})
                else:
                    yield held_result
            else:
                tail = " | ".join(s for s in stderr_tail if s.strip())
                if looks_like_auth_error(tail):
                    msg = LOGIN_HINT
                else:
                    code = f" (код {rc})" if rc is not None else ""
                    msg = f"Claude Code завершился без результата{code}."
                    if tail:
                        msg += f" Последние строки stderr: {tail[-800:]}"
                yield RuntimeEvent("error", {"message": msg, "fatal": True})
        finally:
            if proc is not None:
                await _terminate_group(proc)
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(BaseException):
                    await t
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    async def _pump_stderr(proc: asyncio.subprocess.Process, tail: "deque[str]") -> None:
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            tail.append(raw.decode("utf-8", "replace").rstrip()[:500])

    @staticmethod
    async def _feed_stdin(proc: asyncio.subprocess.Process, task: str) -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(task.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()


def iter_events(objs: Iterable[dict[str, Any]]) -> list[RuntimeEvent]:
    """Convenience for tests: translate a sequence of objects."""
    tr = StreamTranslator()
    out: list[RuntimeEvent] = []
    for o in objs:
        out.extend(tr.feed(o))
    out.extend(tr.finish())
    return out
