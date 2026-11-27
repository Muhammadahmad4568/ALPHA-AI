"""tests/fixtures.py - fake assistant, SSE client and a server fixture (real uvicorn if installed, else MiniHost)."""
from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from core.events import EventBus
from ui.asgi import AlphaApp, GuardMiddleware
from config.settings import UI_DIR


class FakeTTS:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class FakeAlpha:
    """Just enough of Alpha for the web layer: snapshot() (can be made to fail or hang) and the control hooks."""

    def __init__(self, bus: EventBus):
        self.bus, self.mode, self.calls, self.tts, self.tasks = bus, "ok", [], FakeTTS(), []
        self.hang_for, self.snapshot_calls, self.extra = 30.0, 0, {}
        self.inflight = self.max_inflight = 0
        self.stt = None

    def snapshot(self) -> Dict[str, Any]:
        self.snapshot_calls += 1
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            return self._snapshot()
        finally:
            self.inflight -= 1

    def _snapshot(self) -> Dict[str, Any]:
        if self.mode == "raise":
            raise RuntimeError("snapshot exploded")
        if self.mode == "hang":
            time.sleep(self.hang_for)
        return {"state": "listening", "settings": {"provider": "test", "version": "4.0"}, "history": [], "logs": [],
                "tasks": [], "quick": [], "unicode": "اردو ✓", **self.extra}

    def _rec(self, name):
        return lambda *a, **k: self.calls.append((name, a, k))

    def __getattr__(self, name):
        if name in ("submit_text", "submit_quick", "set_muted", "set_listening", "set_gain", "set_gate_db",
                    "reset_counters", "recalibrate", "answer_confirm"):
            return self._rec(name)
        raise AttributeError(name)


class SSEClient:
    """Reads /events in a background thread; closing it disconnects like a browser tab closing."""

    def __init__(self, url: str, token: Optional[str] = "x", path: str = "events", params: Optional[dict] = None):
        self.events: List[Dict[str, Any]] = []
        self.raw: List[str] = []
        self.status: Optional[int] = None
        self.ended = False
        self.error: Optional[str] = None
        self._resp = None
        q = dict(params or {})
        if token is not None:
            q["t"] = token
        self._t = threading.Thread(target=self._run, args=(url + path, q), daemon=True)
        self._t.start()

    def _run(self, url: str, params: dict) -> None:
        try:
            self._resp = requests.get(url, params=params, stream=True, timeout=(3, 30))
            self.status = self._resp.status_code
            self._resp.encoding = "utf-8"
            for line in self._resp.iter_lines(decode_unicode=True):
                self.raw.append(line)
                if line and line.startswith("data:"):
                    self.events.append(json.loads(line[5:]))
        except Exception as exc:                 # closed by us, or the server went away
            self.error = type(exc).__name__
        self.ended = True

    def wait_for(self, pred: Callable[[Dict[str, Any]], bool], timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        end = time.time() + timeout
        while time.time() < end:
            for e in list(self.events):
                if pred(e):
                    return e
            if self.ended and not any(pred(e) for e in self.events):
                return None
            time.sleep(0.02)
        return None

    def types(self) -> List[str]:
        return [e.get("type") for e in self.events]

    def wait_ended(self, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end and not self.ended:
            time.sleep(0.02)
        return self.ended

    def close(self) -> None:
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass


class Server:
    """A running dashboard server around a FakeAlpha. kind = 'uvicorn' (real) or 'minihost' (stdlib)."""

    def __init__(self, heartbeat: float = 0.3, snapshot_timeout: float = 3.0, max_streams: int = 32, port: int = 0):
        self.bus = EventBus()
        self.alpha = FakeAlpha(self.bus)
        self.token = secrets.token_urlsafe(8)
        self.app = AlphaApp(self.alpha, self.bus, self.token, UI_DIR / "static", heartbeat=heartbeat,
                            max_streams=max_streams, snapshot_timeout=snapshot_timeout)
        self.guarded = GuardMiddleware(self.app, "127.0.0.1")
        self._start(port, heartbeat, snapshot_timeout, max_streams)

    def _start(self, port: int, heartbeat: float = 0.3, snapshot_timeout: float = 3.0, max_streams: int = 32) -> None:
        try:
            import uvicorn  # noqa: F401
            from ui.server import find_free_port, start_server

            self.kind = "uvicorn"
            chosen = find_free_port("127.0.0.1", port or 18765)
            self.handle = start_server(self.alpha, self.bus, "127.0.0.1", chosen, self.token)
            self.app = self.handle.app                 # the app uvicorn is serving
            self.app.heartbeat, self.app.snapshot_timeout, self.app.max_streams = heartbeat, snapshot_timeout, max_streams
            self.port = chosen
        except ImportError:
            from tests.asgi_host import MiniHost

            self.kind = "minihost"
            self.host = MiniHost(self.guarded, "127.0.0.1", port).start()
            self.port = self.host.port
        self.url = f"http://127.0.0.1:{self.port}/"

    def stop(self) -> None:
        if self.kind == "uvicorn":
            self.handle.stop()
        else:
            self.host.stop()

    def clients(self) -> int:
        return requests.get(self.url + "api/ping", timeout=2).json()["clients"]

    def wait_clients(self, n: int, timeout: float = 4.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.clients() == n:
                return True
            time.sleep(0.05)
        return False
