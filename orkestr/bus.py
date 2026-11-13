"""In-process pub/sub feeding the SSE stream (implements interfaces.Bus).

Each subscriber owns a bounded deque. When it is full the OLDEST item is
dropped, so a slow client can never block or grow memory for the publisher.
`publish` never blocks and never raises. It is safe to call from the event loop
thread and, as a convenience, from other threads (the wake-up is then routed
through `call_soon_threadsafe`).
"""
from __future__ import annotations

import asyncio
import collections
import logging
import threading
from typing import Any

log = logging.getLogger("orkestr.bus")

DEFAULT_QUEUE_SIZE = 1000


class Subscription:
    """Async iterator of (type, data). Registered at creation, not at first use."""

    def __init__(self, bus: "EventBus", maxsize: int, loop: asyncio.AbstractEventLoop) -> None:
        self._bus = bus
        self._items: collections.deque[Any] = collections.deque()
        self._maxsize = maxsize
        self._loop = loop
        self._event = asyncio.Event()
        self._closed = False
        self.dropped = 0

    # -- producer side -------------------------------------------------
    def _push(self, item: tuple[str, dict[str, Any]]) -> None:
        if self._closed:
            return
        if len(self._items) >= self._maxsize:
            self._items.popleft()
            self.dropped += 1
        self._items.append(item)
        self._wake()

    def _wake(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._event.set()
            return
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:  # loop already closed
            pass

    # -- consumer side -------------------------------------------------
    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> tuple[str, dict[str, Any]]:
        while True:
            if self._closed:
                raise StopAsyncIteration
            if self._items:
                return self._items.popleft()
            self._event.clear()
            if self._items or self._closed:
                continue
            await self._event.wait()

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self)
        self._wake()

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # best effort: unsubscribe abandoned iterators
        try:
            self._bus._remove(self)
        except Exception:
            pass


class EventBus:
    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = max(1, queue_size)
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()

    def publish(self, type: str, data: dict[str, Any]) -> None:
        try:
            with self._lock:
                subs = list(self._subs)
            item = (type, data)
            for sub in subs:
                try:
                    sub._push(item)
                except Exception:  # pragma: no cover - defensive
                    log.exception("bus: push failed")
        except Exception:  # pragma: no cover - defensive
            log.exception("bus: publish failed")

    def subscribe(self) -> Subscription:
        """Must be called from a running event loop (the consumer's loop)."""
        sub = Subscription(self, self._queue_size, asyncio.get_running_loop())
        with self._lock:
            self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def close_all(self) -> None:
        """End every subscription (used on shutdown so SSE streams finish)."""
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.close()
