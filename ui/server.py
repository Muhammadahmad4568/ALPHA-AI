"""
ui/server.py - starts, verifies and stops the dashboard server (uvicorn hosting ui/asgi.py).

STARTUP IS VERIFIED, NOT ASSUMED
  After uvicorn reports "started", Alpha opens a real HTTP + SSE connection to itself and checks that /api/ping
  answers, the page carries the session token, and a /events stream delivers its "hello" snapshot. Only then is the
  dashboard window opened. If a check fails (port taken by something else, firewall, a broken snapshot) the server is
  stopped and restarted on the next port, up to 3 times, and the exact failure is printed. A window can therefore never
  be opened onto a backend that cannot stream.

LIFECYCLE
  uvicorn runs in a daemon thread. Stopping closes the event bus first (open SSE streams end immediately), then asks
  uvicorn to exit with a short graceful-shutdown timeout, so Ctrl+C never hangs on a connected browser.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
import webbrowser
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from config.settings import UI_DIR
from core.events import EventBus
from ui.asgi import AlphaApp, GuardMiddleware

log = logging.getLogger("alpha.ui")
STATIC_DIR = UI_DIR / "static"


def find_free_port(host: str, start: int, span: int = 30) -> int:
    for port in range(start, start + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host if host not in ("::",) else "0.0.0.0", port))
                return port
            except OSError:
                continue
    raise OSError(f"No free port found in {start}-{start + span - 1}")


# =============================================================================
# System status (runs on the BackgroundHub's periodic pool)
# =============================================================================
class StatusPublisher:
    def __init__(self, alpha, bus: EventBus, interval: float = 2.0):
        self.alpha, self.bus, self.interval = alpha, bus, interval
        self.latest: Dict[str, Any] = {}
        self._screen: Dict[str, dict] = {}
        self._screen_at = 0.0
        self._primed = False

    def start(self) -> None:
        self.alpha.hub.add_periodic("status", self.interval, self.tick)

    def tick(self) -> None:
        import psutil

        if not self._primed:
            psutil.cpu_percent(None)
            self._primed = True
        self.latest = self._collect(psutil)
        self.bus.publish({"type": "status", **self.latest})

    def _collect(self, psutil) -> Dict[str, Any]:
        from tools.pc_controller import get_active_window

        vm = psutil.virtual_memory()
        win = get_active_window()
        tracker = self.alpha.tracker
        snap = tracker.snapshot() if tracker else None
        return {
            "cpu": round(psutil.cpu_percent(None), 1),
            "ram_percent": round(vm.percent, 1),
            "ram_used_gb": round((vm.total - vm.available) / 1e9, 2),
            "ram_total_gb": round(vm.total / 1e9, 2),
            "alpha_mb": round(psutil.Process().memory_info().rss / 1e6),
            "window": {"app": win[0], "title": win[1]} if win else None,
            "tracking": tracker is not None,
            "screen_time": self._screen_time(snap) if tracker else {},
        }

    def _screen_time(self, snap) -> Dict[str, Any]:
        now = time.time()
        if now - self._screen_at > 10 or not self._screen:  # DB queries every 10 s
            today, mem = date.today(), self.alpha.memory
            ranges = {"today": (today, today), "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
                      "week": (today - timedelta(days=6), today)}
            fresh: Dict[str, Any] = {}
            for key, (a, b) in ranges.items():
                s = mem.activity_summary(a.isoformat(), b.isoformat())
                fresh[key] = {"total": s["total"], "apps": [[n, v] for n, v in s["apps"]]}
            fresh["trend"] = [[d, v] for d, v in mem.activity_daily(7)]
            self._screen, self._screen_at = fresh, now
        out: Dict[str, Any] = {}
        for key in ("today", "yesterday", "week"):
            apps = {n: v for n, v in self._screen[key]["apps"]}
            total = self._screen[key]["total"]
            if snap and key in ("today", "week"):  # add the in-progress segment
                apps[snap[0]] = apps.get(snap[0], 0.0) + snap[2]
                total += snap[2]
            top = sorted(apps.items(), key=lambda kv: kv[1], reverse=True)[:6]
            out[key] = {"total": round(total), "apps": [[n, round(v)] for n, v in top]}
        trend = [list(x) for x in self._screen["trend"]]
        if snap and trend:
            trend[-1][1] = trend[-1][1] + snap[2]
        out["trend"] = [[d, round(v)] for d, v in trend]
        return out


# =============================================================================
# Server runner
# =============================================================================
class WebHandle:
    def __init__(self, server, thread: threading.Thread, url: str, bus: EventBus, app: AlphaApp, errors: List[BaseException]):
        self.server, self.thread, self.url, self.bus, self.app, self.errors = server, thread, url, bus, app, errors

    @property
    def alive(self) -> bool:
        return self.thread.is_alive() and not self.errors

    def stop(self) -> None:
        self.bus.close()                                   # end open event streams first
        self.server.should_exit = True
        self.thread.join(timeout=6)
        if self.thread.is_alive():
            log.warning("Dashboard server did not stop within 6 s (daemon thread; the process will still exit).")


def start_server(alpha, bus: EventBus, host: str, port: int, token: str) -> WebHandle:
    import uvicorn

    app = AlphaApp(alpha, bus, token, STATIC_DIR)
    kwargs: Dict[str, Any] = dict(host=host, port=port, log_level="warning", access_log=False, timeout_keep_alive=30,
                                  lifespan="on", ws="none")
    try:
        config = uvicorn.Config(GuardMiddleware(app, host), timeout_graceful_shutdown=3, **kwargs)
    except TypeError:                                      # older uvicorn without the graceful-shutdown option
        config = uvicorn.Config(GuardMiddleware(app, host), **kwargs)
    server = uvicorn.Server(config)
    errors: List[BaseException] = []

    def serve() -> None:
        try:
            server.run()
        except BaseException as exc:                       # incl. SystemExit(1) uvicorn raises when the port is taken
            errors.append(exc)
            log.error("Dashboard server stopped: %r", exc)

    thread = threading.Thread(target=serve, name="WebServer", daemon=True)
    thread.start()
    for _ in range(100):                                   # up to 10 s for startup
        if server.started and app.ready:
            break
        if not thread.is_alive():
            raise RuntimeError(f"the dashboard server could not start on port {port} "
                               f"({errors[0]!r})" if errors else f"the dashboard server could not start on port {port}")
        time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError(f"the dashboard server did not finish starting on port {port} within 10 s")
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return WebHandle(server, thread, f"http://{shown}:{port}/", bus, app, errors)


def verify_stream(url: str, token: str, timeout: float = 8.0) -> List[str]:
    """Open real HTTP + SSE connections to the running server. Returns a list of problems (empty = healthy)."""
    problems: List[str] = []
    try:
        r = requests.get(url + "api/ping", timeout=3)
        if r.status_code != 200 or r.json().get("app") != "alpha-ai":
            problems.append(f"/api/ping answered {r.status_code} (is another program using this port?)")
    except (requests.RequestException, ValueError) as exc:
        return [f"/api/ping unreachable: {type(exc).__name__} (firewall or antivirus blocking localhost?)"]
    try:
        page = requests.get(url, timeout=3)
        if f'content="{token}"' not in page.text:
            problems.append("the dashboard page does not contain the session token")
        for asset in ("static/app.js", "static/style.css"):
            if requests.get(url + asset, timeout=3).status_code != 200:
                problems.append(f"{asset} is missing (are ui/static/* files in place?)")
    except requests.RequestException as exc:
        problems.append(f"dashboard page unreachable: {type(exc).__name__}")
    try:
        deadline = time.time() + timeout
        with requests.get(url + "events", params={"t": token}, stream=True, timeout=(3, timeout)) as resp:
            if resp.status_code != 200:
                problems.append(f"/events answered {resp.status_code}")
            else:
                got_hello = False
                for raw in resp.iter_lines(decode_unicode=True):
                    if raw and raw.startswith("data:"):
                        event = json.loads(raw[5:])
                        if event.get("type") == "hello":
                            got_hello = True
                            if event.get("degraded"):
                                problems.append(f"the first snapshot is degraded: {event.get('error')}")
                            break
                    if time.time() > deadline:
                        break
                if not got_hello:
                    problems.append("/events connected but never delivered the hello snapshot")
    except (requests.RequestException, ValueError) as exc:
        problems.append(f"/events stream failed: {type(exc).__name__}: {exc}")
    return problems


def start_dashboard_server(alpha, bus: EventBus, host: str, port: int, token: str, attempts: int = 3,
                           log_fn=print) -> WebHandle:
    """Start, self-verify and (on failure) retry on the next port. Raises RuntimeError with all diagnostics."""
    if getattr(alpha, "status_pub", None) is None:        # system metrics publisher: created once, not per attempt
        alpha.status_pub = StatusPublisher(alpha, bus)
        alpha.status_pub.start()
    failures: List[str] = []
    for i in range(attempts):
        try:
            chosen = find_free_port(host, port + i)
            handle = start_server(alpha, bus, host, chosen, token)
        except Exception as exc:
            failures.append(f"attempt {i + 1}: {exc}")
            continue
        problems = verify_stream(handle.url, token)
        if not problems:
            if i:
                log_fn(f"[i] Port {port} was unavailable; the dashboard is on port {chosen}.")
            return handle
        failures.append(f"attempt {i + 1} (port {chosen}): " + "; ".join(problems))
        log.error("Dashboard self-check failed: %s", problems)
        handle.stop()
    raise RuntimeError("dashboard self-check failed -> " + " | ".join(failures))


def _find_chromium_browser() -> Optional[str]:
    """msedge.exe / chrome.exe on Windows (Edge ships with Windows 10/11)."""
    try:
        import winreg

        for exe in ("msedge.exe", "chrome.exe"):
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                        path = os.path.expandvars(str(winreg.QueryValueEx(k, None)[0])).strip('"')
                        if os.path.exists(path):
                            return path
                except OSError:
                    continue
    except ImportError:
        pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            if base and os.path.exists(os.path.join(base, rel)):
                return os.path.join(base, rel)
    return None


def open_dashboard(url: str, app_window: bool = True) -> None:
    """Open as a frameless desktop-style window (Edge/Chrome --app), else the default browser."""
    exe = _find_chromium_browser() if app_window else None
    if exe:
        try:
            profile = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AlphaAI" / "app-window"
            subprocess.Popen([exe, f"--app={url}", "--window-size=1360,860", f"--user-data-dir={profile}",
                              "--no-first-run", "--no-default-browser-check"])
            return
        except OSError:
            log.info("Could not start the app window; using the default browser.")
    try:
        webbrowser.open(url)
    except Exception:
        log.debug("Could not open a browser", exc_info=True)