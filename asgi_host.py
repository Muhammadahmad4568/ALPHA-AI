"""
tests/asgi_host.py - a tiny HTTP/1.1 ASGI host (standard library only).

Used by the stream tests when uvicorn is not installed (for example in a sandbox). It implements what the dashboard
needs and what uvicorn does: request parsing, lifespan startup/shutdown, chunked streaming responses, an
http.disconnect message when the client goes away, and silently ignoring writes after a disconnect.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, Optional, Set


class MiniHost:
    def __init__(self, app, host: str = "127.0.0.1", port: int = 0):
        self.app, self.host, self._port = app, host, port
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server = None
        self._writers: Set[asyncio.StreamWriter] = set()
        self._ready = threading.Event()
        self._lifespan_q: "asyncio.Queue" = None  # type: ignore
        self._lifespan_task = None
        self._lifespan_done: "asyncio.Event" = None  # type: ignore
        self.started = False
        self.error: Optional[BaseException] = None

    @property
    def port(self) -> int:
        return self._port

    # -------------------------------------------------------------- lifecycle
    def start(self) -> "MiniHost":
        self._thread = threading.Thread(target=self._run, daemon=True, name="MiniHost")
        self._thread.start()
        if not self._ready.wait(10) or self.error:
            raise RuntimeError(f"MiniHost failed to start: {self.error}")
        return self

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
            self.loop.run_forever()
        except BaseException as exc:
            self.error = exc
            self._ready.set()
        finally:
            self.loop.close()

    async def _serve(self) -> None:
        self._lifespan_q, self._lifespan_done = asyncio.Queue(), asyncio.Event()
        startup_done = asyncio.Event()

        async def receive():
            return await self._lifespan_q.get()

        async def send(message: Dict[str, Any]) -> None:
            if message["type"] == "lifespan.startup.complete":
                startup_done.set()
            elif message["type"] == "lifespan.shutdown.complete":
                self._lifespan_done.set()

        self._lifespan_task = asyncio.ensure_future(self.app({"type": "lifespan"}, receive, send))
        await self._lifespan_q.put({"type": "lifespan.startup"})
        await asyncio.wait_for(startup_done.wait(), 5)
        self._server = await asyncio.start_server(self._handle, self.host, self._port)
        self._port = self._server.sockets[0].getsockname()[1]
        self.started = True
        self._ready.set()

    def stop(self, timeout: float = 5.0) -> None:
        if not self.loop or not self.loop.is_running():
            return

        async def shutdown() -> None:
            if self._server:
                self._server.close()
            await self._lifespan_q.put({"type": "lifespan.shutdown"})      # app closes the bus -> streams end
            try:
                await asyncio.wait_for(self._lifespan_done.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.2)
            for w in list(self._writers):
                w.close()

        fut = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        try:
            fut.result(timeout + 2)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=3)

    # ------------------------------------------------------------------ http
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        watcher = None
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            except Exception:
                return
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = [(k.strip().lower().encode("latin-1"), v.strip().encode("latin-1"))
                       for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)]
            hmap = dict(headers)
            length = int(hmap.get(b"content-length", b"0"))
            body = await reader.readexactly(length) if length else b""
            path, _, qs = target.partition("?")
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
                     "path": path, "raw_path": path.encode(), "query_string": qs.encode(), "root_path": "",
                     "headers": headers, "client": writer.get_extra_info("peername"), "server": (self.host, self._port)}
            disconnected = asyncio.Event()
            sent_body = False

            async def receive() -> Dict[str, Any]:
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def watch_eof() -> None:
                try:
                    while await reader.read(1024):
                        pass
                except Exception:
                    pass
                disconnected.set()

            watcher = asyncio.ensure_future(watch_eof())
            state = {"chunked": False}

            async def send(message: Dict[str, Any]) -> None:
                if disconnected.is_set():
                    return                                  # uvicorn ignores writes after a disconnect
                try:
                    if message["type"] == "http.response.start":
                        hs = message.get("headers", [])
                        state["chunked"] = not any(k.lower() == b"content-length" for k, _ in hs)
                        out = f"HTTP/1.1 {message['status']} X\r\n".encode()
                        for k, v in hs:
                            out += k + b": " + v + b"\r\n"
                        out += (b"Transfer-Encoding: chunked\r\n" if state["chunked"] else b"") + b"Connection: close\r\n\r\n"
                        writer.write(out)
                    elif message["type"] == "http.response.body":
                        data = message.get("body", b"")
                        if state["chunked"]:
                            if data:
                                writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                            if not message.get("more_body"):
                                writer.write(b"0\r\n\r\n")
                        else:
                            writer.write(data)
                    await writer.drain()
                except (ConnectionError, OSError):
                    disconnected.set()

            try:
                await self.app(scope, receive, send)
            except Exception:
                pass
        finally:
            if watcher:
                watcher.cancel()
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass
