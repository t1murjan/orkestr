"""ProbeManager: background hardware sampling and model-server probing.

Implements ``interfaces.Probes``. All writes go through the store
(``add_hardware_sample`` / ``save_server_status``), which publishes the SSE
events itself.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .hardware import HardwareCollector
from .servers import LOCAL_KINDS, ServerProber, make_status

log = logging.getLogger("orkestr.probes")


class _Sched:
    __slots__ = ("sig", "next_due", "fails", "lock")

    def __init__(self, sig: tuple) -> None:
        self.sig = sig
        self.next_due = 0.0
        self.fails = 0
        self.lock = asyncio.Lock()


class ProbeManager:
    def __init__(self, store: Any, *, hardware_interval_s: float = 2.0, server_interval_s: float = 10.0,
                 max_backoff_s: float = 60.0, collector: HardwareCollector | None = None,
                 prober: ServerProber | None = None) -> None:
        self._store = store
        self._hw_interval = hardware_interval_s
        self._srv_interval = server_interval_s
        self._max_backoff = max_backoff_s
        self._collector = collector
        self._prober = prober
        self._latest_hw: dict[str, Any] | None = None
        self._sched: dict[str, _Sched] = {}
        self._tasks: list[asyncio.Task] = []
        self._stop: asyncio.Event | None = None
        self._inflight: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._tasks:
            return
        self._stop = asyncio.Event()
        if self._collector is None:
            self._collector = HardwareCollector()
        if self._prober is None:
            self._prober = ServerProber()
        self._tasks = [
            asyncio.create_task(self._hardware_loop(), name="probes-hardware"),
            asyncio.create_task(self._server_loop(), name="probes-servers"),
        ]

    async def stop(self) -> None:
        if not self._tasks:
            return
        assert self._stop is not None
        self._stop.set()
        tasks = self._tasks + list(self._inflight)
        self._tasks = []
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        if self._prober is not None:
            try:
                await self._prober.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._prober = None

    async def _sleep(self, seconds: float) -> None:
        assert self._stop is not None
        try:
            await asyncio.wait_for(self._stop.wait(), max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------ hardware
    def latest_hardware(self) -> dict[str, Any] | None:
        return self._latest_hw

    async def _hardware_loop(self) -> None:
        assert self._stop is not None and self._collector is not None
        try:
            await self._collector.prime()
        except Exception:  # noqa: BLE001
            log.debug("hardware prime failed", exc_info=True)
        await self._sleep(min(0.3, self._hw_interval))
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                sample = await self._collector.sample()
                self._latest_hw = sample
                self._store.add_hardware_sample(sample)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("hardware sample failed", exc_info=True)
            await self._sleep(self._hw_interval - (time.monotonic() - started))

    # ------------------------------------------------------------------ servers
    def local_servers_up(self) -> list[dict[str, Any]]:
        try:
            servers = self._store.list_servers()
        except Exception:  # noqa: BLE001
            return []
        return [
            s for s in servers
            if s.get("kind") in LOCAL_KINDS and s.get("enabled", True)
            and isinstance(s.get("status"), dict) and s["status"].get("state") == "up"
        ]

    def _delay_after(self, fails: int) -> float:
        if fails <= 0:
            return self._srv_interval
        return min(self._max_backoff, self._srv_interval * (2 ** fails))

    async def _probe_and_save(self, server: dict[str, Any], sched: _Sched) -> dict[str, Any] | None:
        """Probe + persist; never raises (except cancellation). Returns saved Server."""
        async with sched.lock:
            assert self._prober is not None
            try:
                status = await self._prober.probe(server)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("probe crashed for %s", server.get("id"), exc_info=True)
                status = make_status("down", error="Внутренняя ошибка проверки сервера.")
            sched.fails = 0 if status.get("state") == "up" else sched.fails + 1
            sched.next_due = time.monotonic() + self._delay_after(sched.fails)
            try:
                return self._store.save_server_status(server["id"], status)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("saving server status failed", exc_info=True)
                return None

    def _sched_for(self, server: dict[str, Any]) -> _Sched:
        sig = (server.get("kind"), server.get("base_url"))
        s = self._sched.get(server["id"])
        if s is None:
            s = self._sched[server["id"]] = _Sched(sig)
        elif s.sig != sig:  # edited: forget backoff, probe now
            s.sig, s.fails, s.next_due = sig, 0, 0.0
        return s

    async def check_server(self, server_id: str) -> dict[str, Any] | None:
        """Probe now (ignores backoff) and return the fresh Server object."""
        try:
            server = self._store.get_server(server_id)
        except Exception:  # noqa: BLE001
            return None
        if not server:
            return None
        if self._prober is None:  # not started: use a throwaway prober
            prober = ServerProber()
            try:
                status = await prober.probe(server)
            finally:
                await prober.aclose()
            return self._store.save_server_status(server_id, status)
        saved = await self._probe_and_save(server, self._sched_for(server))
        return saved if saved is not None else self._store.get_server(server_id)

    async def _run_one(self, server: dict[str, Any], sched: _Sched) -> None:
        try:
            await self._probe_and_save(server, sched)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("server probe task failed", exc_info=True)

    async def _server_loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                servers = self._store.list_servers()
            except Exception:  # noqa: BLE001
                log.warning("list_servers failed", exc_info=True)
                servers = []
            ids = {s["id"] for s in servers}
            for gone in [i for i in self._sched if i not in ids]:
                del self._sched[gone]
            now = time.monotonic()
            due: list[asyncio.Task] = []
            for s in servers:
                if not s.get("enabled", True):
                    continue
                sched = self._sched_for(s)
                if sched.next_due <= now and not sched.lock.locked():
                    t = asyncio.create_task(self._run_one(s, sched), name=f"probe-{s['id']}")
                    self._inflight.add(t)
                    t.add_done_callback(self._inflight.discard)
                    due.append(t)
            # do not wait for probes: slow servers must not delay fast ones
            await self._sleep(min(0.5, max(0.01, self._srv_interval / 2)))
