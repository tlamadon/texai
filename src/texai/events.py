"""A tiny in-process pub/sub bus for streaming agent and build events to the UI.

Server-Sent Events have no replay, so the bus also keeps a bounded backlog: a
client that reconnects asks for everything after the last id it saw and then
tails the live stream, exactly like the viewer's PDF-version polling.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

__all__ = ["Event", "EventBus", "format_sse", "sse_stream"]

BACKLOG_SIZE = 500
SUBSCRIBER_QUEUE_SIZE = 1000
KEEPALIVE_SECONDS = 15.0


@dataclass(frozen=True)
class Event:
    id: int
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, **self.data}


class EventBus:
    """Fan-out of events to any number of SSE subscribers."""

    def __init__(self, backlog: int = BACKLOG_SIZE) -> None:
        self._next_id = 1
        self._backlog: list[Event] = []
        self._backlog_size = backlog
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def publish(self, type: str, **data: Any) -> Event:
        event = Event(id=self._next_id, type=type, data=data)
        self._next_id += 1

        self._backlog.append(event)
        if len(self._backlog) > self._backlog_size:
            del self._backlog[: len(self._backlog) - self._backlog_size]

        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A subscriber that cannot keep up is dropped; its client will
                # reconnect and replay from the backlog.
                self._subscribers.discard(queue)
        return event

    def since(self, last_id: int) -> list[Event]:
        return [event for event in self._backlog if event.id > last_id]

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


def format_sse(event: Event) -> str:
    """One event in Server-Sent Events wire format."""
    return f"id: {event.id}\ndata: {json.dumps(event.as_dict())}\n\n"


async def sse_stream(
    bus: EventBus, since: int = 0, keepalive: float = KEEPALIVE_SECONDS
) -> AsyncIterator[str]:
    """Backlog after ``since``, then the live tail, as SSE frames.

    Subscribing before replaying the backlog is what makes the handover
    lossless: anything published while we are replaying is already queued, and
    the id check drops the overlap.
    """
    queue = bus.subscribe()
    last_id = since
    try:
        for event in bus.since(last_id):
            last_id = event.id
            yield format_sse(event)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=keepalive)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event.id <= last_id:
                continue  # already replayed from the backlog
            last_id = event.id
            yield format_sse(event)
    finally:
        bus.unsubscribe(queue)
