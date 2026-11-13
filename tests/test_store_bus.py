"""EventBus behaviour."""
from __future__ import annotations

import asyncio

from orkestr.bus import EventBus


async def test_publish_and_receive_in_order():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish("a", {"n": 1})
    bus.publish("b", {"n": 2})
    assert await sub.__anext__() == ("a", {"n": 1})
    assert await sub.__anext__() == ("b", {"n": 2})
    sub.close()
    assert bus.subscriber_count == 0


async def test_waiting_consumer_wakes_up():
    bus = EventBus()
    sub = bus.subscribe()
    task = asyncio.ensure_future(sub.__anext__())
    await asyncio.sleep(0.01)
    assert not task.done()
    bus.publish("x", {})
    assert await asyncio.wait_for(task, 1) == ("x", {})


async def test_slow_subscriber_drops_oldest_and_never_blocks():
    bus = EventBus(queue_size=3)
    slow, fast = bus.subscribe(), bus.subscribe()
    got = []
    for i in range(10):
        bus.publish("e", {"i": i})
        got.append((await fast.__anext__())[1]["i"])
    assert got == list(range(10))
    seen = [(await slow.__anext__())[1]["i"] for _ in range(3)]
    assert seen == [7, 8, 9] and slow.dropped == 7


async def test_no_subscribers_and_close_ends_iteration():
    bus = EventBus()
    bus.publish("x", {})  # nobody listens: fine
    sub = bus.subscribe()
    task = asyncio.ensure_future(sub.__anext__())
    await asyncio.sleep(0)
    bus.close_all()
    try:
        await asyncio.wait_for(task, 1)
        raise AssertionError("should have stopped")
    except StopAsyncIteration:
        pass
    bus.publish("y", {})  # still never raises


async def test_publish_from_another_thread():
    bus = EventBus()
    sub = bus.subscribe()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, bus.publish, "t", {"v": 1})
    assert await asyncio.wait_for(sub.__anext__(), 1) == ("t", {"v": 1})
