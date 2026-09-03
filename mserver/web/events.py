"""A tiny publish/subscribe bus for streaming dashboard updates.

The dashboard used to be a static page you had to reload. This lets the
browser watch the agent work in real time over Server-Sent Events.

Why SSE rather than WebSockets
------------------------------
Everything here is one-directional: the server talks, the browser listens.
SSE is plain HTTP, needs no extra dependency, reconnects on its own, and
works through the sort of proxy a phone is likely to sit behind. A WebSocket
would mean either a dependency or hand-rolling a framing protocol, for a
feature that never needs the client to speak.

Why a bounded queue per subscriber
----------------------------------
A browser tab that is throttled (phone screen off, background tab) stops
reading. With an unbounded queue that slowly eats the process's memory on a
device with 4 GB and no swap. Each subscriber therefore gets a small ring:
when it overflows the oldest events are dropped and the client is told, which
is the right trade for a progress display.
"""
from __future__ import annotations

import json
import queue
import threading
import time

MAX_QUEUE = 200          # events held for a slow subscriber before dropping
MAX_SUBSCRIBERS = 8      # a phone does not need more, and this bounds memory
HEARTBEAT_SECONDS = 20   # keeps intermediaries from closing an idle stream


class EventBus:
    """Fan-out of dashboard events to any number of listening browsers."""

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._seq = 0

    # ------------------------------------------------------------ publishing
    def publish(self, kind: str, data: dict | None = None) -> dict:
        """Send an event to every subscriber. Never raises."""
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "kind": kind, "t": time.time(),
                     "data": data or {}}
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # Drop the oldest rather than the newest: a progress view
                    # cares about what is happening now.
                    try:
                        q.get_nowait()
                        q.put_nowait(event)
                    except (queue.Empty, queue.Full):
                        dead.append(q)
            for q in dead:
                if q in self._subs:
                    self._subs.remove(q)
        return event

    # ----------------------------------------------------------- subscribing
    def subscribe(self) -> queue.Queue | None:
        """Returns a queue of events, or None if there are already too many."""
        with self._lock:
            if len(self._subs) >= MAX_SUBSCRIBERS:
                return None
            q: queue.Queue = queue.Queue(maxsize=MAX_QUEUE)
            self._subs.append(q)
            return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


def sse_format(event: dict) -> bytes:
    """Encode one event in the Server-Sent Events wire format."""
    payload = json.dumps(event.get("data", {}), default=str)
    return (f"id: {event.get('seq', 0)}\n"
            f"event: {event.get('kind', 'message')}\n"
            f"data: {payload}\n\n").encode()


class LiveEvents(list):
    """A list that also publishes whatever is appended to it.

    `Agent.ask()` already collects tool calls into a plain list. Passing one
    of these instead means the web chat streams progress live without the
    agent core knowing anything about the dashboard — the alternative was
    threading a callback through several layers of unrelated code.
    """

    def __init__(self, bus: EventBus, kind: str = "tool"):
        super().__init__()
        self._bus = bus
        self._kind = kind

    def append(self, item) -> None:
        super().append(item)
        try:
            self._bus.publish(self._kind, item if isinstance(item, dict)
                              else {"value": str(item)})
        except Exception:
            pass          # streaming must never break the agent turn
