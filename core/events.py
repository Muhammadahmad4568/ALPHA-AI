"""
core/events.py - [RECONSTRUCTED] thread-safe pub/sub event bus between Alpha's worker threads and the
dashboard's asyncio event loop (uvicorn). Also a logging.Handler so log records reach the Console tab.

NOTE: this file's original content was never provided during this debugging session - it is rebuilt
from every usage site seen in ui/asgi.py, core/assistant.py, main.py, and tests/fixtures.py:
  - EventBus()                           no-arg constructor
  - bus.publish(dict)                    broadcast an event
  - bus.subscribe(loop) -> asyncio.Queue  register a new SSE client bound to its own event loop
  - bus.unsubscribe(queue)                drop a client
  - bus.subscriber_count                  int property, how many SSE clients are connected
  - bus.snapshot() -> {"history": [...], "logs": [...], "latest": {...}}
  - bus.close()                           end every open stream (puts None on each subscriber's queue)
  - BusLogHandler(bus)                    a logging.Handler that republishes log records as "log" events

WHY call_soon_threadsafe: publish() is called from arbitrary background threads (the voice listener,
BackgroundHub workers, etc.) while each subscriber's queue belongs to the asyncio loop running inside
uvicorn's own thread. asyncio.Queue.put_nowait() is not safe to call directly from a different thread,
so every publish schedules the put on each subscriber's own loop instead of touching its queue directly.

If you have your project's ORIGINAL core/events.py anywhere (a backup, git history, another machine),
use that instead and ignore this file - this is a best-effort match, not a guaranteed byte-for-byte
copy of what you started with.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, Dict, Optional

# Event types whose most recent instance is worth remembering for a reconnecting client's "hello" snapshot.
_LATEST_KINDS = ("confirm", "confirm_done", "state", "settings", "mic", "status", "voice_auth")


class EventBus:
    def __init__(self, history: int = 200, logs: int = 300):
        self._lock = threading.Lock()
        self._subs: Dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}
        self._history: "deque[dict]" = deque(maxlen=history)
        self._logs: "deque[dict]" = deque(maxlen=logs)
        self._latest: Dict[str, dict] = {}
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            closed = self._closed
            if not closed:
                self._subs[q] = loop
        if closed:
            loop.call_soon_threadsafe(q.put_nowait, None)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.pop(q, None)

    def publish(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("ts", time.time())
        with self._lock:
            if self._closed:
                return
            etype = event.get("type")
            if etype == "message":
                self._history.append(event)
            elif etype == "log":
                self._logs.append(event)
            if etype in _LATEST_KINDS:
                self._latest[etype] = event
            subs = list(self._subs.items())
        for q, loop in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:
                pass  # that loop is already closed; unsubscribe() will clean it up separately

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"history": list(self._history), "logs": list(self._logs), "latest": dict(self._latest)}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            subs = list(self._subs.items())
            self._subs.clear()
        for q, loop in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, None)
            except RuntimeError:
                pass


class BusLogHandler(logging.Handler):
    """Feeds Python logging records into the EventBus so the dashboard's Console tab can show them."""

    def __init__(self, bus: EventBus, level: int = logging.INFO):
        super().__init__(level)
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bus.publish({"type": "log", "level": record.levelname, "name": record.name,
                              "msg": record.getMessage()})
        except Exception:
            pass