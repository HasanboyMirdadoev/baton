from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, Optional, Set


class EventBus:
    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue[Dict[str, Any]]] = set()

    async def publish(self, event_name: str, data: Dict[str, Any]) -> None:
        """
        Pushes {"event": event_name, "data": data} to every active subscriber queue
        using put_nowait (ignoring QueueFull if a slow client lags).
        """
        message: Dict[str, Any] = {"event": event_name, "data": data}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, heartbeat_sec: float = 15.0) -> AsyncIterator[Dict[str, Any]]:
        """
        Async generator yielding {"event": str, "data": dict} items.
        If no event arrives within `heartbeat_sec`, yields {"event": "ping", "data": {}}.
        Removes its queue from `self._subscribers` in a `finally:` block on disconnect/cancel.
        """
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_sec)
                    yield event
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": {}}
        finally:
            self._subscribers.discard(queue)
