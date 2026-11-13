# Оркестр — technical contract

Single source of truth for every module. Code contracts live in
`orkestr/runtimes/base.py` and `orkestr/interfaces.py` (both frozen). This file
defines the product, the data objects, the HTTP API, the SSE stream and file
ownership. If you find a gap or a contradiction, do not silently invent a
different shape: pick the smallest reasonable reading, implement it, and list it
in your final report under "Contract deviations".

## 1. Product

«Оркестр» is a local web control panel (Linux, binds 127.0.0.1) for one person
who works with AI coding agents. In the browser they:

1. Create **agents** (a named role bound to a runtime: Claude Code or OpenCode,
   a model, a project folder, permissions).
2. Draw the **hierarchy** on a canvas: supervisor -> subordinate links, made
   with the mouse, effective immediately.
3. Give an agent a **task**. The backend launches the real CLI headless. Every
   agent with subordinates gets MCP tools to **delegate** work to them, so a
   Claude Code agent can hand work to an OpenCode agent and the other way round.
4. **Watch**: what each agent is doing right now, how full its context window
   is (shown on hover), tokens, cost, the delegation tree and timeline of every
   run, plus hardware load and local model servers (LM Studio, llama.cpp).
5. Also see Claude Code sessions the user started in a terminal (read only).

UI language: Russian. All user-visible strings (UI, error messages, activity
phrases) are Russian. Code, identifiers, comments, logs: English.

Runtimes: `claude_code` (cloud models via the user's Claude Code login),
`opencode` (any provider OpenCode has, including local LM Studio / llama.cpp),
`demo` (built-in simulator, clearly labelled as demo everywhere).

## 2. Objects

All JSON. Timestamps: integer epoch **milliseconds**. Unknown = `null`.

### 2.1 Agent

```
{ id, name,                      // name unique, 1..40 chars
  runtime: "claude_code"|"opencode"|"demo",
  model: string|null,            // runtime specific id, null = runtime default
  role: string,                  // one line: what this agent is for (shown to supervisors)
  system_prompt: string,         // extra instructions, may be ""
  cwd: string|null,              // project folder; null = settings.default_cwd
  permissions: "read"|"edit"|"full",   // default "edit"
  color: string|null,            // optional accent key chosen by user: null|"a".."f"
  x: number, y: number,          // canvas position
  demo: boolean,
  created_at, updated_at,
  live: {                        // computed by the store, read only
    status: "idle"|"working"|"delegating"|"error",
    active_runs: int,
    current_run_id: string|null, // newest running run
    activity: string|null,       // Russian phrase, e.g. "Читает src/app.py"
    context_used: int|null, context_window: int|null,   // of current or last run
    model: string|null,          // model actually used by current/last run
    tokens_in: int, tokens_out: int, cost_usd: number,  // lifetime totals
    runs_total: int, last_run_at: int|null,
    last_status: "ok"|"error"|"cancelled"|null } }
```

`live.status`: `delegating` when a running run is waiting on a running
delegation step; `working` when any run is running; `error` when nothing runs
and the last finished run failed; else `idle`.

### 2.2 Edge

`{ id, supervisor_id, subordinate_id, created_at }` — no self links, no
duplicates, no cycles (the graph is a DAG; an agent may have several
supervisors and several subordinates).

### 2.3 Run — one execution of one agent on one task

```
{ id, agent_id: string|null,     // null for external sessions
  agent_name, runtime, model,
  task: string,
  status: "queued"|"running"|"ok"|"error"|"cancelled",
  origin: "user"|"delegation"|"external",
  parent_run_id, root_run_id, depth: int,   // root: parent null, root_run_id = id, depth 0
  parent_step_id: string|null,   // the delegation step in the parent run
  session_id: string|null, cwd: string|null,
  started_at, ended_at: int|null, duration_ms: int|null,
  result: string|null, error: string|null,
  activity: string|null,         // Russian phrase for what it does now
  tokens_in, tokens_out, cache_read_tokens, cache_write_tokens: int,
  cost_usd: number,
  context_used: int|null, context_window: int|null,
  num_turns: int, tool_calls: int, delegations: int,
  demo: boolean }
```

### 2.4 Step — one entry on a run's timeline

```
{ id, run_id, seq: int,
  kind: "message"|"thinking"|"tool"|"delegation"|"system"|"error",
  name: string,                  // raw tool name, or "assistant", "thinking", ...
  title: string,                 // Russian human label, e.g. "Читает src/app.py"
  status: "running"|"ok"|"error",
  started_at, ended_at: int|null, duration_ms: int|null,
  input: object|null,            // tool input (large strings truncated to 4000 chars)
  output: string|null,           // text / tool output, truncated to 20000 chars
  tool_id: string|null,
  child_run_id: string|null }    // delegation: the subordinate's run
```

### 2.5 Server — a model server being probed

```
{ id, name, kind: "lmstudio"|"llamacpp"|"openai_compat"|"anthropic",
  base_url, enabled: boolean, builtin: boolean, created_at,
  status: null | {
    state: "up"|"down"|"unknown",
    checked_at, latency_ms: int|null, error: string|null,   // error in Russian
    version: string|null,
    models: [ { id, state: "loaded"|"available"|null,
                context_window: int|null,       // currently loaded context length
                max_context_window: int|null,
                arch: string|null, quantization: string|null,
                size_bytes: int|null, type: string|null } ],
    slots: [ { id: int, state: "idle"|"processing",        // llama.cpp only
               context_window: int|null, context_used: int|null,
               model: string|null } ],
    metrics: { tokens_per_s: number|null, requests_processing: int|null,
               requests_deferred: int|null, prompt_tokens_total: int|null,
               generated_tokens_total: int|null } | null } }
```

Seeded on first start (`builtin: true`, can be disabled, not deleted):
LM Studio `http://127.0.0.1:1234`, llama.cpp `http://127.0.0.1:8080`,
Anthropic API `https://api.anthropic.com` (reachability only, no key is ever
sent or stored). `location` is derived: `anthropic` is cloud, the rest local.

### 2.6 Hardware sample

```
{ ts, cpu_percent: number, cpu_count: int, load_avg: [n,n,n],
  ram_used_bytes, ram_total_bytes, swap_used_bytes, swap_total_bytes,
  gpus: [ { index, name, vendor: "nvidia"|"amd"|"intel"|null,
            util_percent: number|null,
            mem_used_bytes: int|null, mem_total_bytes: int|null,
            temp_c: number|null, power_w: number|null } ],
  processes: [ { pid, kind: "claude_code"|"opencode"|"lmstudio"|"llamacpp"|"ollama",
                 name, cpu_percent, rss_bytes, gpu_mem_bytes: int|null } ] }
```

### 2.7 Runtime info

`RuntimeInfo.to_dict()` from `runtimes/base.py`:
`{ id, label, available, version, path, error, models: [ModelInfo] }`.

### 2.8 Settings

```
{ max_depth: 3,                // deepest delegation level (root = 0)
  max_concurrent_runs: 4,      // more runs wait as "queued"
  delegate_wait_s: 120,        // how long the delegate tool blocks before returning a task id
  run_timeout_s: 1800,
  default_cwd: string,         // default: directory the server was started in
  watch_external: true,        // observe terminal Claude Code sessions
  demo_auto: false }           // demo driver launches demo tasks periodically
```

## 3. Errors

HTTP errors: `{ "error": { "code": string, "message": string (Russian) } }`.
Codes: `not_found` 404, `validation` 422, `unknown_agent` 404, `self_edge` 409,
`duplicate_edge` 409, `cycle` 409, `name_taken` 409, `runtime_unavailable` 409,
`bad_cwd` 422, `depth_exceeded` 409, `empty_task` 422, `builtin` 409.

## 4. HTTP API (prefix `/api/v1`)

| Method & path | Body / query | Returns |
| --- | --- | --- |
| GET `/state` | | `{version, demo_seeded, agents, edges, runtimes, servers, settings, hardware: sample or null}` |
| GET `/agents` | | `{agents:[Agent]}` |
| POST `/agents` | Agent fields (name, runtime required) | Agent |
| PATCH `/agents/{id}` | partial | Agent |
| DELETE `/agents/{id}` | | `{ok:true}` (cancels its active runs) |
| GET `/edges` | | `{edges:[Edge]}` |
| POST `/edges` | `{supervisor_id, subordinate_id}` | Edge |
| DELETE `/edges/{id}` | | `{ok:true}` |
| GET `/runs` | `status, agent_id, origin, root_only, limit, before` | `{runs:[Run]}` newest first |
| POST `/runs` | `{agent_id, task, cwd?}` | Run |
| GET `/runs/{id}` | | `{run, tree:[Run], steps:[Step]}` — tree = whole tree of the run's root; steps of all runs in the tree |
| POST `/runs/{id}/cancel` | | `{ok:true}` |
| GET `/overview` | `window_s=3600, buckets=60` | §4.1 |
| GET `/runtimes` | | `{runtimes:[RuntimeInfo]}` |
| POST `/runtimes/refresh` | | same, after re-detection |
| GET `/servers` | | `{servers:[Server]}` |
| POST `/servers` | `{name, kind, base_url}` | Server |
| PATCH `/servers/{id}` | partial (`name, base_url, enabled`) | Server |
| DELETE `/servers/{id}` | | `{ok:true}` (`builtin` -> 409) |
| POST `/servers/{id}/check` | | Server (fresh status) |
| GET `/hardware` | `window_s=600` | `{latest: sample or null, samples:[sample]}` at most 300 points, `processes` only in `latest` |
| GET `/settings` | | Settings |
| PATCH `/settings` | partial | Settings |
| GET `/fs/dirs` | `path` (default: home) | `{path, parent, dirs:[{name, path}]}` — sub-directories only, hidden ones skipped, for the folder picker |
| POST `/demo/seed` | | `{agents, edges}` — creates the demo team (idempotent) |
| POST `/demo/clear` | | `{ok:true}` — removes demo agents, their edges and runs |
| GET `/stream` | | SSE, §5 |
| GET `/connect` | | `{mcp: {agent_url_template, claude_command_template, opencode_snippet_template}}` — ready-to-copy instructions for attaching a terminal session to an agent |

### 4.1 Overview

```
{ window_s, now,
  kpis: { active_runs, runs, runs_ok, runs_error, delegations, tool_calls,
          tokens_in, tokens_out, cost_usd, avg_run_ms, p95_run_ms,
          // same metrics for the previous window of equal length, for "vs previous" deltas
          prev: { runs, runs_error, tokens_in, tokens_out, cost_usd, avg_run_ms } },
  series: { t:[bucket start ms], runs:[], errors:[], tokens_in:[], tokens_out:[],
            cost_usd:[], active:[] },          // arrays of equal length = buckets
  by_agent: [ { agent_id, agent_name, runtime, runs, errors, tokens_in, tokens_out,
                cost_usd, avg_run_ms } ],      // sorted by runs desc
  by_model: [ { model, runtime, runs, errors, tokens_in, tokens_out, cost_usd,
                avg_run_ms } ],
  by_runtime: [ { runtime, runs, errors, tokens_in, tokens_out, cost_usd } ] }
```

Runs are attributed to the bucket of `started_at`; `active` = runs running at
the bucket end.

## 5. SSE stream `GET /api/v1/stream`

`event: <type>` + `data: <JSON>`. First event after connect: `hello`
`{version, now}`. `ping` `{now}` every 15 s. Object events carry the full
object and are published by the Store on every mutation:

| type | data |
| --- | --- |
| `agent` | Agent (also re-published when one of its runs changes its `live`) |
| `agent.deleted` | `{id}` |
| `edge` / `edge.deleted` | Edge / `{id}` |
| `run` | Run |
| `step` | Step |
| `server` / `server.deleted` | Server / `{id}` |
| `hardware` | Hardware sample |
| `runtimes` | `{runtimes:[RuntimeInfo]}` |
| `settings` | Settings |
| `demo.cleared` | `{}` (clients reload `/state`) |

The engine throttles streaming text so one step is updated at most ~5 times a
second. Clients must tolerate missed events: on reconnect they reload `/state`
and the open view's data.

## 6. Delegation over MCP

The backend serves MCP (Streamable HTTP, JSON responses) at:

- `/mcp/run/{token}` — unique per run, token unguessable, valid while the run is
  active. Passed to the adapter as `RunSpec.mcp_url`.
- `/mcp/agent/{agent_id}` — static identity, for a session the user started in a
  terminal and attached to an agent of the hierarchy.

Tools (server name `orkestr`):

- `list_subordinates()` -> the caller's current subordinates:
  `[{name, role, runtime, model, status}]`. Reads the live hierarchy, so links
  drawn a second ago are already there.
- `delegate(agent: string, task: string)` -> starts a run of that subordinate
  with `origin: "delegation"` and waits up to `settings.delegate_wait_s`.
  Finished: returns the subordinate's result text. Still running: returns
  `{status:"running", task_id}` and tells the model to call `check_task`.
  Refuses (as a tool error with a clear message) when the agent is not a
  current subordinate, when `max_depth` would be exceeded, or the runtime is
  unavailable.
- `check_task(task_id: string, wait_s?: int)` -> status or result.

A run of an agent with no subordinates gets `mcp_url = None` (no tools at all).
The engine links the delegation: the parent's tool step for `delegate` becomes
`kind: "delegation"` with `child_run_id`, the child has `parent_run_id` and
`parent_step_id`. Cancelling a run cancels its descendants.

## 7. File ownership

Only edit files you own. Read anything.

| Owner | Files |
| --- | --- |
| orchestrator (frozen) | `orkestr/runtimes/base.py`, `orkestr/interfaces.py`, `pyproject.toml`, `docs/` |
| core | `orkestr/store.py`, `bus.py`, `api.py`, `app.py`, `config.py`, `__main__.py`, `tests/test_store*.py`, `tests/test_api*.py` |
| engine | `orkestr/engine.py`, `mcp_server.py`, `activity.py`, `demo.py`, `runtimes/__init__.py`, `runtimes/demo.py`, `tests/test_engine*.py`, `tests/test_mcp*.py`, `tests/fakes.py` |
| claude | `orkestr/runtimes/claude_code.py`, `orkestr/watchers/claude_sessions.py`, `tests/test_claude*.py`, `tests/fixtures/claude/` |
| opencode | `orkestr/runtimes/opencode.py`, `tests/test_opencode*.py`, `tests/fixtures/opencode/`, `tests/tools/mock_openai_server.py` |
| probes | `orkestr/probes/*.py`, `tests/test_probes*.py`, `tests/tools/fake_model_servers.py` |
| web | `web/**` |

### Fixed names other modules import

```
orkestr/bus.py          class EventBus                      # implements interfaces.Bus
orkestr/store.py        class SqliteStore(path: str, bus)   # implements interfaces.Store
orkestr/engine.py       class RunEngine(store, bus, adapters: dict[str, RuntimeAdapter],
                                         probes=None, *, base_url: str)   # interfaces.Engine
orkestr/mcp_server.py   def build_mcp_app(engine, store) -> ASGI app      # mounted at /mcp
orkestr/activity.py     def describe_tool(name: str, input: dict, mcp: dict|None) -> str   # Russian phrase
orkestr/demo.py         def seed_demo(store) -> dict        # {agents, edges}, idempotent
                        def clear_demo(store, engine) -> None   (async)
                        class DemoDriver(engine, store)     # async start()/stop(); obeys settings.demo_auto
orkestr/runtimes/__init__.py   def build_adapters() -> dict[str, RuntimeAdapter]
orkestr/runtimes/claude_code.py   class ClaudeCodeAdapter(RuntimeAdapter)
orkestr/runtimes/opencode.py      class OpenCodeAdapter(RuntimeAdapter)
orkestr/runtimes/demo.py          class DemoAdapter(RuntimeAdapter)
orkestr/watchers/claude_sessions.py   class ClaudeSessionWatcher(engine, store, *, projects_dir=None, poll_s=2.0)
                                      # async start()/stop(); obeys settings.watch_external
orkestr/probes/manager.py   class ProbeManager(store, *, hardware_interval_s=2.0, server_interval_s=10.0)
                            # implements interfaces.Probes
orkestr/app.py          def create_app(config) -> FastAPI
orkestr/config.py       class Config (host, port, db_path, demo, web_dir, ...)
```

`build_adapters()` imports each adapter inside try/except and logs a failure,
so one broken adapter never prevents startup.

Run: `python -m orkestr [--host 127.0.0.1] [--port 8787] [--db PATH] [--demo]`.
`--demo` seeds the demo team and turns `demo_auto` on.

## 8. Engineering rules

- Python 3.11+, type hints, `asyncio`. Use the shared venv:
  `/home/user/orkestr/.venv/bin/python` (fastapi, uvicorn, httpx, psutil, mcp,
  pytest, pytest-asyncio installed). Need another package: say so in the report
  rather than installing silently; stdlib first.
- No `shell=True`, no string-built commands. Validate paths. Never store, log
  or return API keys or auth headers.
- Every module has pytest tests that run offline and fast:
  `.venv/bin/python -m pytest tests/test_<yours>*.py -q`. Tests must not touch
  the user's real `~/.claude`, `~/.config/opencode` etc. — use tmp dirs.
- Other agents are writing sibling modules **at the same time** in this
  directory. A module you depend on may not exist yet: code against
  `interfaces.py` / `base.py`, use your own fakes in tests, and when the real
  file appears, try it. Never edit or "fix" a file you do not own; report it.
- Do not run `git commit`, do not start long-lived servers on port 8787 (the
  orchestrator uses it). Use the test port assigned in your brief and stop
  whatever you start.
- Verify external tool behaviour (CLI flags, output formats, HTTP APIs) against
  the real tool or its current documentation, not from memory. Record what you
  verified and how in the module docstring.
- Final report: what you built, what you verified against reality and how,
  what is unverified, contract deviations, known gaps. Keep it under 400 words.
