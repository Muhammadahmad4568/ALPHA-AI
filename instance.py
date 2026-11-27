"""
core/instance.py - only one Alpha at a time.

A second copy would fight the first one for the microphone and the dashboard port, and an old dashboard window
would keep talking to the OLD process (different token) while the new one hides on another port. That looks exactly
like "Reconnecting - Alpha's backend isn't answering". So: a pid lock file plus a port probe, with a clear message.
"""
from __future__ import annotations

import atexit
import os
from pathlib import Path
from typing import Optional

import requests

from config.settings import DATA_DIR


class AlreadyRunning(Exception):
    def __init__(self, pid: int, url: str = ""):
        self.pid, self.url = pid, url
        super().__init__(f"Alpha is already running (process {pid})" + (f" at {url}" if url else ""))


def probe_port(host: str, port: int) -> Optional[dict]:
    """Returns the /api/ping answer if an Alpha dashboard is already listening there."""
    h = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        r = requests.get(f"http://{h}:{port}/api/ping", timeout=1.0)
        data = r.json()
        return data if data.get("app") == "alpha-ai" else None
    except (requests.RequestException, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return False
        name = psutil.Process(pid).name().lower()
        return "python" in name or "alpha" in name        # a recycled pid owned by another program is not us
    except Exception:
        return False


class InstanceLock:
    def __init__(self, path: Path = DATA_DIR / "alpha.lock"):
        self.path, self._held = path, False

    def acquire(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        found = probe_port(host, port)
        if found and found.get("pid") != os.getpid():
            raise AlreadyRunning(int(found.get("pid") or 0), f"http://127.0.0.1:{port}/")
        try:
            pid = int(self.path.read_text().strip())
            if pid != os.getpid() and _alive(pid):
                raise AlreadyRunning(pid)
        except (OSError, ValueError):
            pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()))
        self._held = True
        atexit.register(self.release)

    def release(self) -> None:
        if self._held:
            try:
                if self.path.read_text().strip() == str(os.getpid()):
                    self.path.unlink()
            except OSError:
                pass
            self._held = False
