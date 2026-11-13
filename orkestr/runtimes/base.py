"""Shared contract between the engine and runtime adapters.

FROZEN: owned by the orchestrator. Adapters and the engine import from here and
must not change it. If something is missing, report it instead of editing.

A runtime adapter wraps one agent CLI (Claude Code, OpenCode, demo) and turns a
single run into a stream of normalized RuntimeEvent objects.
"""
from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Literal

EventType = Literal[
    "session",     # runtime session established
    "thinking",    # model reasoning text (optional)
    "text",        # assistant visible text
    "tool_start",  # tool call started
    "tool_end",    # tool call finished
    "usage",       # token usage of ONE model turn (deltas)
    "result",      # terminal: final answer of the run
    "error",       # problem report; data.fatal=True ends the run as error
    "log",         # diagnostic line, not shown as a step
]


@dataclass(slots=True)
class RuntimeEvent:
    """One normalized event. `data` shape by type:

    session:    {session_id: str, model: str|None, context_window: int|None}
    thinking:   {text: str, delta: bool}
    text:       {text: str, delta: bool}
                delta=True  -> append to the assistant message in progress
                delta=False -> one complete assistant message block
    tool_start: {tool_id: str, name: str, input: dict,
                 mcp: {"server": str, "tool": str} | None}
                `name` is the raw tool name of the runtime ("Read", "bash",
                "mcp__orkestr__delegate", ...). `mcp` is set when the tool
                belongs to an MCP server, with the bare server and tool names.
    tool_end:   {tool_id: str, output: str, is_error: bool}
                output truncated by the adapter to at most 20_000 chars.
    usage:      {input_tokens: int, output_tokens: int,
                 cache_read_tokens: int, cache_write_tokens: int,
                 context_used: int|None, context_window: int|None,
                 cost_usd: float|None}
                Token counts and cost_usd are DELTAS for one model turn.
                context_used = tokens currently occupying the context window
                after this turn (prompt incl. cache + output of this turn).
    result:     {text: str, is_error: bool, cost_usd: float|None,
                 num_turns: int|None}
                Exactly one per run, last event. cost_usd here is the TOTAL
                for the run and, when not None, overrides summed deltas.
    error:      {message: str, fatal: bool}
    log:        {level: "debug"|"info"|"warn"|"error", message: str}
    """

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)


Permissions = Literal["read", "edit", "full"]
"""read: read/search files in cwd only, no edits, no shell, no network tools.
edit: read + create/edit files inside cwd; shell stays disabled.
full: everything allowed without confirmation (user opted in per agent)."""


@dataclass(slots=True)
class RunSpec:
    run_id: str
    agent: dict[str, Any]          # Agent object, docs/CONTRACT.md §2.1
    task: str                      # the prompt for this run
    cwd: str                       # existing absolute directory
    model: str | None              # runtime-specific model id, None = runtime default
    system_prompt: str             # extra system prompt composed by the engine ("" if none)
    permissions: Permissions
    mcp_url: str | None            # Streamable-HTTP MCP endpoint with the delegation
                                   # tools for THIS run; None = run without delegation
    mcp_server_name: str = "orkestr"
    mcp_tool_timeout_s: int = 900  # the CLI must not time out MCP tool calls sooner
    env: dict[str, str] = field(default_factory=dict)       # extra env for the subprocess
    local_servers: list[dict[str, Any]] = field(default_factory=list)
    """Server objects (docs/CONTRACT.md §2.5) of kind lmstudio/llamacpp/openai_compat
    that are currently up. An adapter may use them to configure local providers."""


@dataclass(slots=True)
class ModelInfo:
    id: str                        # value to store in agent.model
    label: str
    provider: str                  # "anthropic", "lmstudio", "llamacpp", "openai", ...
    location: Literal["local", "cloud"]
    context_window: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RuntimeInfo:
    id: str
    label: str
    available: bool
    version: str | None = None
    path: str | None = None
    error: str | None = None       # why it is unavailable, human readable (Russian)
    models: list[ModelInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RuntimeAdapter(abc.ABC):
    """One instance per runtime, created once at startup with no arguments."""

    id: str      # "claude_code" | "opencode" | "demo"
    label: str   # "Claude Code" | "OpenCode" | "Демо"

    @abc.abstractmethod
    async def detect(self, local_servers: list[dict[str, Any]] | None = None) -> RuntimeInfo:
        """Find the CLI, its version and the models it can use. Never raises.
        `local_servers` are Server objects that are currently up, so local
        models can be listed. Should finish within ~10 s."""

    @abc.abstractmethod
    def run(self, spec: RunSpec) -> AsyncIterator[RuntimeEvent]:
        """Async generator that executes one run.

        Rules:
        - Yield exactly one `result` event as the last event, unless a fatal
          `error` event was yielded (then stop without a result).
        - Never raise for expected failures (CLI missing, non-zero exit, bad
          output): yield error(fatal=True) with a clear Russian message.
        - Start the CLI with asyncio.create_subprocess_exec (never a shell),
          start_new_session=True. When the generator is closed or cancelled,
          terminate the whole process group: SIGTERM, then SIGKILL after 3 s.
        - Enforce spec.permissions with the CLI's own permission controls.
        - If spec.mcp_url is set, register it for this run only (never edit the
          user's global config files) under spec.mcp_server_name.
        - Never log or yield secrets (API keys, tokens, auth headers).
        """
