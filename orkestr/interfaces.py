"""Protocols that tie the backend modules together.

FROZEN: owned by the orchestrator. Implementations live in store.py, bus.py,
engine.py, probes/manager.py. Code against these, not against the concrete
classes, so modules written in parallel fit together.

Objects are plain dicts with the shapes of docs/CONTRACT.md §2. Timestamps are
integer epoch milliseconds. Ids are short opaque strings.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Protocol

from .runtimes.base import RuntimeEvent


class Bus(Protocol):
    """In-process pub/sub that feeds the SSE stream."""

    def publish(self, type: str, data: dict[str, Any]) -> None:
        """Non-blocking, callable from the event loop thread. Never raises."""

    def subscribe(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Async iterator of (type, data). Slow subscribers drop oldest events."""


class StoreError(ValueError):
    """Validation failure. `code` is one of the codes of docs/CONTRACT.md §3."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class Store(Protocol):
    """SQLite persistence. Synchronous, fast, safe to call from the event loop.

    EVERY mutation publishes the matching SSE event on the bus by itself
    (see docs/CONTRACT.md §5), so callers never publish object events.
    Returned dicts are full API objects (agents include `live`).
    """

    # settings
    def get_settings(self) -> dict[str, Any]: ...
    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]: ...

    # agents
    def list_agents(self) -> list[dict[str, Any]]: ...
    def get_agent(self, agent_id: str) -> dict[str, Any] | None: ...
    def get_agent_by_name(self, name: str) -> dict[str, Any] | None: ...
    def create_agent(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def update_agent(self, agent_id: str, patch: dict[str, Any]) -> dict[str, Any] | None: ...
    def delete_agent(self, agent_id: str) -> bool: ...   # also deletes its edges

    # hierarchy
    def list_edges(self) -> list[dict[str, Any]]: ...
    def create_edge(self, supervisor_id: str, subordinate_id: str) -> dict[str, Any]: ...
    """Raises StoreError: unknown_agent | self_edge | duplicate_edge | cycle."""
    def delete_edge(self, edge_id: str) -> bool: ...
    def subordinates_of(self, agent_id: str) -> list[dict[str, Any]]: ...  # Agent objects
    def supervisors_of(self, agent_id: str) -> list[dict[str, Any]]: ...   # Agent objects

    # runs
    def create_run(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def update_run(self, run_id: str, patch: dict[str, Any]) -> dict[str, Any] | None: ...
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    def list_runs(
        self,
        *,
        status: str | None = None,       # single status or comma separated list
        agent_id: str | None = None,
        origin: str | None = None,
        root_only: bool = False,
        limit: int = 50,
        before: int | None = None,       # started_at < before (ms), for paging
    ) -> list[dict[str, Any]]: ...       # newest first
    def run_tree(self, root_run_id: str) -> list[dict[str, Any]]: ...  # root + all descendants

    # steps
    def create_step(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def update_step(self, step_id: str, patch: dict[str, Any]) -> dict[str, Any] | None: ...
    def get_step(self, step_id: str) -> dict[str, Any] | None: ...
    def list_steps(self, run_ids: list[str]) -> list[dict[str, Any]]: ...  # ordered by started_at, seq

    # model servers
    def list_servers(self) -> list[dict[str, Any]]: ...
    def get_server(self, server_id: str) -> dict[str, Any] | None: ...
    def create_server(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def update_server(self, server_id: str, patch: dict[str, Any]) -> dict[str, Any] | None: ...
    def delete_server(self, server_id: str) -> bool: ...
    def save_server_status(self, server_id: str, status: dict[str, Any]) -> dict[str, Any] | None: ...
    """Stores status (CONTRACT §2.5), keeps a latency sample, returns the Server."""

    # hardware
    def add_hardware_sample(self, sample: dict[str, Any]) -> None: ...
    def list_hardware_samples(self, since_ms: int, max_points: int = 300) -> list[dict[str, Any]]: ...

    # statistics
    def overview(self, window_s: int = 3600, buckets: int = 60) -> dict[str, Any]: ...


class EngineError(Exception):
    """`code`: unknown_agent | runtime_unavailable | bad_cwd | depth_exceeded |
    not_subordinate | unknown_run | empty_task"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class Engine(Protocol):
    """Runs agents through runtime adapters and implements delegation."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...   # cancels every active run

    async def start_run(
        self,
        agent_id: str,
        task: str,
        *,
        cwd: str | None = None,          # default: agent.cwd, then settings.default_cwd
        origin: str = "user",            # "user" | "delegation"
        parent_run_id: str | None = None,
        parent_step_id: str | None = None,
    ) -> dict[str, Any]:
        """Creates the run (status "queued" or "running") and returns it at once.
        Raises EngineError."""

    async def cancel_run(self, run_id: str) -> bool: ...   # the run and its descendants
    async def wait_run(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Returns the Run once terminal, or its current state after `timeout` seconds."""

    # runtimes
    def runtimes_info(self) -> list[dict[str, Any]]: ...          # cached RuntimeInfo dicts
    async def refresh_runtimes(self) -> list[dict[str, Any]]: ... # re-detect, publish "runtimes"

    # external sessions observed by watchers (not launched by us)
    def external_begin(self, key: str, info: dict[str, Any]) -> str:
        """Idempotent per `key`. info: {runtime, title, cwd, model, session_id,
        started_at, parent_key (optional, for sub-agents)}. Returns run id of a
        Run with origin "external"."""
    def external_event(self, run_id: str, event: RuntimeEvent, ts: int | None = None) -> None: ...
    def external_end(self, run_id: str, status: str = "ok", ts: int | None = None) -> None: ...


class Probes(Protocol):
    """Hardware and model-server monitoring (probes/manager.py: ProbeManager)."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def check_server(self, server_id: str) -> dict[str, Any] | None: ...  # fresh Server object
    def latest_hardware(self) -> dict[str, Any] | None: ...
    def local_servers_up(self) -> list[dict[str, Any]]: ...  # for RunSpec.local_servers
