"""
ui/asgi.py - the dashboard's HTTP layer as a plain ASGI application (no web framework).

WHY NOT A FRAMEWORK'S STREAMING RESPONSE
  The recurring "Reconnecting - Alpha's backend isn't answering" screen came from the stream layer: framework
  streaming responses + middleware that wrap or buffer bodies, a disconnect check racing with the framework's own
  receive() loop, and a first event (the snapshot) that could raise and kill the stream before sending anything.
  Here every stream has ONE reader of receive(), a snapshot that can fail without killing the stream, and a
  data heartbeat the browser can watch. Uvicorn hosts this app unchanged.

ROUTES   GET /                 dashboard (token injected)        GET /static/<file>   css / js
         GET /events           Server-Sent Events                POST /api/control    commands from the page
         GET /api/snapshot     full state (JSON)                 GET /api/ping        liveness probe, no secrets
STREAM   retry hint -> padding (defeats buffering proxies / antivirus) -> hello snapshot -> events;
         a {"type":"ping"} DATA event every `heartbeat` s (SSE comments are invisible to JavaScript, so the page
         could not tell "idle" from "dead"); the stream ends cleanly on client disconnect or server shutdown.
LIFESPAN startup/shutdown handled; shutdown closes the event bus so open streams end and uvicorn can exit.
SECURITY pure-ASGI GuardMiddleware: Host allow-list (DNS rebinding), same-origin POSTs, security headers, no CORS,
         no websockets. Every API route needs the per-run token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from core.events import EventBus

log = logging.getLogger("alpha.ui")
SERVER_ID = secrets.token_hex(4)          # changes on every start: lets the page notice a restarted backend
Scope = Dict[str, Any]


# =============================================================================
# Request policy (pure functions: unit-tested)
# =============================================================================
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0", "[::]", "::"}
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _host_only(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    return (h.split("]")[0] + "]") if h.startswith("[") else h.split(":")[0]


def host_check(host_header: str, bound_host: str) -> Tuple[bool, str]:
    """Accepts every local spelling (localhost, 127.x, [::1], 0.0.0.0) and blocks DNS-rebinding hostnames."""
    h = _host_only(host_header)
    if h in _LOCAL_HOSTS or re.match(r"^127\.\d+\.\d+\.\d+$", h):
        return True, "local host"
    extra = {x.strip().lower() for x in os.environ.get("ALPHA_WEB_ALLOWED_HOSTS", "").split(",") if x.strip()}
    if h in extra:
        return True, "configured host"
    if bound_host not in ("127.0.0.1", "localhost", "::1") and _IPV4.match(h):
        return True, "LAN address"
    return False, (f"Host header '{host_header}' is not allowed - open the dashboard via localhost or 127.0.0.1, "
                   "or add the name to ALPHA_WEB_ALLOWED_HOSTS")


def origin_ok(origin: Optional[str], host_header: str) -> Tuple[bool, str]:
    """For POSTs: browsers always send Origin and it must be this very server (blocks cross-site requests)."""
    if not origin:
        return True, "no Origin header (script / same-origin)"
    if urlparse(origin).netloc.lower() == (host_header or "").lower():
        return True, "same origin"
    extra = {x.strip().rstrip("/").lower() for x in os.environ.get("ALPHA_ALLOWED_ORIGINS", "").split(",") if x.strip()}
    if origin.rstrip("/").lower() in extra:
        return True, "configured origin"
    return False, f"Origin '{origin}' is not allowed"


def token_ok(supplied: str, token: str) -> bool:
    return bool(supplied) and secrets.compare_digest(supplied.encode("utf-8", "ignore"), token.encode("utf-8", "ignore"))


def extract_token(headers: Dict[str, str], query: Dict[str, str]) -> str:
    bearer = headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return headers.get("x-alpha-token") or query.get("t", "") or ""


def handle_client_message(alpha, msg: Dict[str, Any]) -> None:
    """Apply one control message (WebSocket or POST /api/control). Quick and non-blocking."""
    kind = msg.get("type")
    if kind == "command":
        text = str(msg.get("text", ""))[:500].strip()
        if text:
            alpha.submit_text(text)
    elif kind == "quick":
        alpha.submit_quick(str(msg.get("id", "")))
    elif kind == "set_muted":
        alpha.set_muted(bool(msg.get("value")))
    elif kind == "set_listening":
        alpha.set_listening(bool(msg.get("value")))
    elif kind == "set_gain":
        gain = msg.get("gain")
        alpha.set_gain(auto=msg.get("auto"), gain=float(gain) if isinstance(gain, (int, float)) else None)
    elif kind == "set_gate_db":
        value = msg.get("value")
        if isinstance(value, (int, float)):
            alpha.set_gate_db(float(value))
    elif kind == "reset_counters":
        alpha.reset_counters()
    elif kind == "recalibrate":
        alpha.recalibrate()
    elif kind == "confirm":
        alpha.answer_confirm(bool(msg.get("answer")))
    elif kind == "stop_speaking":
        alpha.tts.stop()


# =============================================================================
# ASGI plumbing
# =============================================================================
def sse_pack(event: Dict[str, Any]) -> str:
    try:
        return "data: " + json.dumps(event, default=str, ensure_ascii=False) + "\n\n"
    except (TypeError, ValueError):                      # an unserialisable event must never kill the stream
        return "data: " + json.dumps({"type": "error", "msg": "unserialisable event dropped"}) + "\n\n"


def _headers(scope: Scope) -> Dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _query(scope: Scope) -> Dict[str, str]:
    return {k: v[0] for k, v in parse_qs(scope.get("query_string", b"").decode("latin-1")).items()}


async def respond(send, status: int, body: bytes = b"", content_type: str = "application/json",
                  extra: Optional[List[Tuple[bytes, bytes]]] = None) -> None:
    headers = [(b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode())] + (extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def respond_json(send, status: int, obj: Any) -> None:
    await respond(send, status, json.dumps(obj, default=str).encode("utf-8"))


SECURITY_HEADERS = [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY")]


class GuardMiddleware:
    """Pure ASGI (never wraps or buffers bodies, so it cannot interfere with the event stream)."""

    def __init__(self, app, bound_host: str):
        self.app, self.bound_host = app, bound_host

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":                       # we use no websockets
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        headers = _headers(scope)
        ok, reason = host_check(headers.get("host", ""), self.bound_host)
        if ok and scope["method"] == "POST":
            ok, reason = origin_ok(headers.get("origin"), headers.get("host", ""))
        if not ok:
            log.warning("Rejected %s %s: %s", scope["method"], scope.get("path"), reason)
            return await respond_json(send, 403, {"error": "forbidden", "reason": reason})

        async def guarded_send(message: Scope) -> None:
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                message = dict(message, headers=list(message.get("headers", [])) +
                               [h for h in SECURITY_HEADERS if h[0] not in have])
            await send(message)

        await self.app(scope, receive, guarded_send)


# =============================================================================
# The application
# =============================================================================
class AlphaApp:
    def __init__(self, alpha, bus: EventBus, token: str, static_dir: Path, heartbeat: float = 5.0,
                 max_streams: int = 32, snapshot_timeout: float = 3.0):
        self.alpha, self.bus, self.token, self.static_dir = alpha, bus, token, Path(static_dir)
        self.heartbeat, self.max_streams, self.snapshot_timeout = heartbeat, max_streams, snapshot_timeout
        self.started = time.time()
        self.ready = False
        self._snap_future: Optional["asyncio.Future"] = None

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            return await self._lifespan(receive, send)
        if scope["type"] != "http":
            return
        try:
            await self._route(scope, receive, send)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unhandled error serving %s", scope.get("path"))
            try:
                await respond_json(send, 500, {"error": "internal error"})
            except Exception:
                pass

    # ---------------------------------------------------------------- lifespan
    async def _lifespan(self, receive, send) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                self.ready = True
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                self.ready = False
                self.bus.close()                           # open streams end now, so uvicorn can exit promptly
                await send({"type": "lifespan.shutdown.complete"})
                return

    # ------------------------------------------------------------------ routes
    def _authorised(self, scope: Scope) -> bool:
        ok = token_ok(extract_token(_headers(scope), _query(scope)), self.token)
        if not ok:
            log.warning("Rejected %s %s: missing or stale token (the page reloads itself to fetch a new one)",
                        scope["method"], scope.get("path"))
        return ok

    async def _route(self, scope: Scope, receive, send) -> None:
        method, path = scope["method"], scope.get("path", "/")
        if path == "/api/ping" and method == "GET":
            return await respond_json(send, 200, {"ok": True, "app": "alpha-ai", "version": "4.0", "pid": os.getpid(),
                                                  "server_id": SERVER_ID, "uptime": round(time.time() - self.started),
                                                  "clients": self.bus.subscriber_count})
        if path == "/events" and method == "GET":
            return await self._events(scope, receive, send)
        if path == "/api/snapshot" and method == "GET":
            if not self._authorised(scope):
                return await self._unauthorised(send)
            return await respond_json(send, 200, await self._snapshot())
        if path == "/api/control" and method == "POST":
            return await self._control(scope, receive, send)
        if path == "/" and method == "GET":
            html = (self.static_dir / "index.html").read_text(encoding="utf-8").replace("__ALPHA_TOKEN__", self.token)
            return await respond(send, 200, html.encode("utf-8"), "text/html; charset=utf-8")
        if path.startswith("/static/") and method == "GET":
            return await self._static(path[len("/static/"):], send)
        await respond_json(send, 405 if path in ("/", "/events", "/api/ping", "/api/snapshot", "/api/control") else 404,
                           {"error": "not found"})

    @staticmethod
    async def _unauthorised(send) -> None:
        await respond_json(send, 401, {"error": "unauthorized", "reason": "token missing or stale - reload the page"})

    async def _static(self, rel: str, send) -> None:
        target = (self.static_dir / rel).resolve()
        if self.static_dir.resolve() not in target.parents or not target.is_file():   # blocks ../ traversal
            return await respond_json(send, 404, {"error": "not found"})
        ctype = {".js": "application/javascript", ".css": "text/css", ".html": "text/html"}.get(
            target.suffix, mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        await respond(send, 200, target.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/javascript")) else ""))

    async def _control(self, scope: Scope, receive, send) -> None:
        if not self._authorised(scope):
            return await self._unauthorised(send)
        body = b""
        while True:                                        # read the body in pieces, with a hard size cap
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            if len(body) > 4096:
                return await respond_json(send, 413, {"error": "too large"})
            if not msg.get("more_body"):
                break
        try:
            message = json.loads(body or b"{}")
        except ValueError:
            return await respond_json(send, 400, {"error": "invalid json"})
        if not isinstance(message, dict):
            return await respond_json(send, 400, {"error": "expected an object"})
        try:
            handle_client_message(self.alpha, message)
        except Exception:
            log.exception("Error handling control message %r", message.get("type"))
            return await respond_json(send, 500, {"error": "failed"})
        await respond_json(send, 200, {"ok": True})

    # ---------------------------------------------------------------- snapshot
    async def _snapshot(self) -> Dict[str, Any]:
        """Full state. Never raises and never hangs: a broken or slow snapshot degrades to a minimal one."""
        loop = asyncio.get_running_loop()
        fut = self._snap_future
        if fut is None or fut.done():                      # at most ONE call in flight: a hung snapshot must not
            fut = self._snap_future = loop.run_in_executor(None, self.alpha.snapshot)   # eat a thread per reconnect
        try:
            data = await asyncio.wait_for(asyncio.shield(fut), self.snapshot_timeout)
            return {"server_id": SERVER_ID, **data}
        except asyncio.TimeoutError:
            log.error("alpha.snapshot() took more than %.1f s; sending a minimal snapshot", self.snapshot_timeout)
            return {"server_id": SERVER_ID, "degraded": True, "state": "idle", "error": "snapshot timed out"}
        except Exception as exc:
            log.exception("alpha.snapshot() failed")
            return {"server_id": SERVER_ID, "degraded": True, "state": "idle", "error": f"{type(exc).__name__}: {exc}"}

    # --------------------------------------------------------------------- SSE
    async def _events(self, scope: Scope, receive, send) -> None:
        if not self._authorised(scope):
            return await self._unauthorised(send)
        if self.bus.subscriber_count >= self.max_streams:
            return await respond(send, 503, b'{"error":"too many open streams"}', "application/json",
                                 [(b"retry-after", b"3")])
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"text/event-stream; charset=utf-8"), (b"cache-control", b"no-cache, no-transform"),
            (b"x-accel-buffering", b"no"), (b"x-content-type-options", b"nosniff")]})
        loop = asyncio.get_running_loop()
        q = self.bus.subscribe(loop)
        disconnected = asyncio.Event()

        async def watch() -> None:                         # the ONLY consumer of receive() for this stream
            while True:
                if (await receive())["type"] == "http.disconnect":
                    disconnected.set()
                    return

        async def write(text: str) -> None:
            await send({"type": "http.response.body", "body": text.encode("utf-8"), "more_body": True})

        watcher, getter = asyncio.ensure_future(watch()), None
        try:
            await write(": " + " " * 2048 + "\n\n")         # padding: some proxies/antivirus buffer small chunks
            await write("retry: 1500\n\n")
            await write(sse_pack({"type": "hello", **(await self._snapshot())}))
            getter = asyncio.ensure_future(q.get())
            while True:
                done, _ = await asyncio.wait({getter, watcher}, timeout=self.heartbeat,
                                             return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    break                                  # browser went away
                if getter in done:
                    event = getter.result()
                    getter = asyncio.ensure_future(q.get())
                    if event is None:
                        break                              # server shutting down
                    await write(sse_pack(event))
                else:                                      # idle: prove we are alive (visible to JavaScript)
                    await write(sse_pack({"type": "ping", "ts": time.time(), "server_id": SERVER_ID,
                                          "clients": self.bus.subscriber_count}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:                           # a failed write means the client is gone
            log.debug("Event stream ended: %s", exc)
        finally:
            for task in (getter, watcher):
                if task is not None and not task.done():
                    task.cancel()
            self.bus.unsubscribe(q)
            try:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            except Exception:
                pass
