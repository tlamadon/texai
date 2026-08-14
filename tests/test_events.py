import asyncio
import json

from texai.events import EventBus, format_sse, sse_stream


async def take(stream, count: int, timeout: float = 2.0) -> list[str]:
    """Pull ``count`` frames off an endless stream, then close it."""
    frames: list[str] = []
    try:
        for _ in range(count):
            frames.append(await asyncio.wait_for(anext(stream), timeout=timeout))
    finally:
        await stream.aclose()
    return frames


def payload(frame: str) -> dict:
    line = next(part for part in frame.splitlines() if part.startswith("data: "))
    return json.loads(line[len("data: ") :])


# ------------------------------------------------------------------- the bus


def test_ids_increment_from_one():
    bus = EventBus()
    assert bus.publish("a").id == 1
    assert bus.publish("b").id == 2


def test_since_returns_only_newer_events():
    bus = EventBus()
    bus.publish("a")
    bus.publish("b")
    bus.publish("c")
    assert [event.type for event in bus.since(1)] == ["b", "c"]
    assert bus.since(3) == []
    assert len(bus.since(0)) == 3


def test_backlog_is_bounded():
    bus = EventBus(backlog=3)
    for index in range(10):
        bus.publish("tick", index=index)
    backlog = bus.since(0)
    assert len(backlog) == 3
    assert [event.data["index"] for event in backlog] == [7, 8, 9]


def test_event_payload_is_flat():
    bus = EventBus()
    event = bus.publish("agent_text", turnId="t0001", text="hi")
    assert event.as_dict() == {"id": 1, "type": "agent_text", "turnId": "t0001", "text": "hi"}


def test_subscribers_receive_published_events():
    bus = EventBus()
    queue = bus.subscribe()
    bus.publish("a")
    assert queue.get_nowait().type == "a"
    bus.unsubscribe(queue)
    bus.publish("b")
    assert queue.empty()


def test_slow_subscriber_is_dropped_rather_than_blocking():
    """A stalled client must not wedge the agent loop."""
    bus = EventBus()
    queue = bus.subscribe()
    for _ in range(queue.maxsize + 5):
        bus.publish("tick")
    assert bus.subscriber_count == 0  # dropped; the client reconnects and replays


# ---------------------------------------------------------------- sse framing


def test_format_sse_shape():
    bus = EventBus()
    frame = format_sse(bus.publish("agent_text", text="hi"))
    assert frame.startswith("id: 1\ndata: ")
    assert frame.endswith("\n\n")
    assert payload(frame)["text"] == "hi"


async def test_stream_replays_backlog_after_since():
    bus = EventBus()
    bus.publish("first")
    bus.publish("second")
    bus.publish("third")

    frames = await take(sse_stream(bus, since=1), 2)
    assert [payload(f)["type"] for f in frames] == ["second", "third"]


async def test_stream_continues_with_live_events():
    bus = EventBus()
    bus.publish("backlog")

    stream = sse_stream(bus, since=0)
    first = await asyncio.wait_for(anext(stream), timeout=2)
    assert payload(first)["type"] == "backlog"

    bus.publish("live")
    second = await asyncio.wait_for(anext(stream), timeout=2)
    assert payload(second)["type"] == "live"
    await stream.aclose()


async def test_stream_does_not_duplicate_across_the_handover():
    """Events published while the backlog replays must appear exactly once."""
    bus = EventBus()
    bus.publish("one")
    stream = sse_stream(bus, since=0)

    first = await asyncio.wait_for(anext(stream), timeout=2)  # subscribes, then replays
    bus.publish("two")
    second = await asyncio.wait_for(anext(stream), timeout=2)

    assert payload(first)["id"] == 1
    assert payload(second)["id"] == 2
    await stream.aclose()


async def test_stream_emits_keepalives_when_idle():
    bus = EventBus()
    frames = await take(sse_stream(bus, since=0, keepalive=0.01), 2)
    assert all(frame.startswith(": keepalive") for frame in frames)


async def test_stream_unsubscribes_when_closed():
    bus = EventBus()
    bus.publish("a")
    stream = sse_stream(bus, since=0)
    await asyncio.wait_for(anext(stream), timeout=2)
    assert bus.subscriber_count == 1
    await stream.aclose()
    assert bus.subscriber_count == 0
