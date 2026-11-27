"""
background.py - keeps slow work off the speech path.

  BackgroundHub  one asyncio event loop in its own thread with named worker queues:
                   "db"   SQLite writes (activity segments, conversation log)
                   "net"  outbound HTTP (n8n webhooks)
                   "log"  deferred housekeeping
                 plus periodic jobs (system-status collection).
                 submit() is non-blocking and thread-safe; blocking functions run in a
                 dedicated single-thread executor per queue, so jobs on one queue keep
                 their order and never stall the loop or the voice threads.
  Metrics        small thread-safe latency recorder (STT / routing / LLM / TTS ...).
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("alpha.bg")


class BackgroundHub:
    QUEUES = ("db", "net", "log")

    def __init__(self, maxsize: int = 2000):
        self._maxsize = maxsize
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._queues: Dict[str, asyncio.Queue] = {}
        self._pools: Dict[str, concurrent.futures.ThreadPoolExecutor] = {}
        self._periodic: List[Any] = []
        self.stats: Dict[str, Dict[str, int]] = {n: {"done": 0, "failed": 0, "dropped": 0} for n in self.QUEUES}

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="BackgroundHub", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        for name in self.QUEUES:
            self._queues[name] = asyncio.Queue(self._maxsize)
            self._pools[name] = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix=f"bg-{name}")
            loop.create_task(self._worker(name))
        self._pools["periodic"] = concurrent.futures.ThreadPoolExecutor(2, thread_name_prefix="bg-periodic")
        self._ready.set()
        try:
            loop.run_forever()
        finally:  # cancel the workers cleanly so nothing is left pending when the loop closes
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    @property
    def running(self) -> bool:
        return self._loop is not None and self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float = 3.0) -> None:
        """Drain what is already queued (up to `timeout` s), then shut the loop down."""
        if not self.running:
            return
        deadline = time.time() + timeout
        for name in self.QUEUES:
            self.barrier(name, max(0.05, deadline - time.time()))
        for fut in self._periodic:
            fut.cancel()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)
        for pool in self._pools.values():
            pool.shutdown(wait=False)

    # -------------------------------------------------------------- queues
    async def _worker(self, name: str) -> None:
        q, loop, pool = self._queues[name], asyncio.get_running_loop(), self._pools[name]
        while True:
            fn, args, kwargs, done = await q.get()
            try:
                if asyncio.iscoroutinefunction(fn):
                    await fn(*args, **kwargs)
                else:
                    await loop.run_in_executor(pool, lambda: fn(*args, **kwargs))
                self.stats[name]["done"] += 1
            except Exception:
                self.stats[name]["failed"] += 1
                log.exception("Background job failed on queue '%s'", name)
            finally:
                if done is not None:
                    done.set()

    def _enqueue(self, name: str, item: tuple) -> None:  # runs on the hub loop
        try:
            self._queues[name].put_nowait(item)
        except asyncio.QueueFull:
            self.stats[name]["dropped"] += 1
            if item[3] is not None:
                item[3].set()
            log.warning("Background queue '%s' is full; dropped a job.", name)

    def submit(self, queue: str, fn: Callable, *args, **kwargs) -> None:
        """Run fn(*args, **kwargs) in the background. Falls back to inline execution if the hub is down."""
        if not self.running or queue not in self._queues:
            try:
                fn(*args, **kwargs)
            except Exception:
                log.exception("Inline background job failed")
            return
        self._loop.call_soon_threadsafe(self._enqueue, queue, (fn, args, kwargs, None))

    def barrier(self, queue: str, timeout: float = 1.0) -> bool:
        """Block until everything submitted to `queue` before this call has finished."""
        if not self.running or queue not in self._queues:
            return True
        ev = threading.Event()
        self._loop.call_soon_threadsafe(self._enqueue, queue, (lambda: None, (), {}, ev))
        return ev.wait(timeout)

    def depths(self) -> Dict[str, int]:
        return {n: self._queues[n].qsize() for n in self._queues}

    # ------------------------------------------------------------ periodic
    def add_periodic(self, name: str, interval: float, fn: Callable[[], None]) -> None:
        """Call fn() every `interval` seconds on the hub's periodic pool (errors are logged, never fatal)."""
        if not self.running:
            raise RuntimeError("BackgroundHub is not running")

        async def runner() -> None:
            loop = asyncio.get_running_loop()
            while True:
                try:
                    await loop.run_in_executor(self._pools["periodic"], fn)
                except Exception:
                    log.exception("Periodic job '%s' failed", name)
                await asyncio.sleep(interval)

        self._periodic.append(asyncio.run_coroutine_threadsafe(runner(), self._loop))


class Metrics:
    """Rolling latency statistics (milliseconds) and counters."""

    def __init__(self, keep: int = 30):
        self._lock = threading.Lock()
        self._series: Dict[str, "collections.deque[float]"] = collections.defaultdict(lambda: collections.deque(maxlen=keep))
        self.counters: Dict[str, int] = collections.defaultdict(int)

    def record(self, name: str, ms: float) -> None:
        with self._lock:
            self._series[name].append(float(ms))

    def incr(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] += n

    @contextmanager
    def timer(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, (time.perf_counter() - t0) * 1000)

    def summary(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            out: Dict[str, Dict[str, float]] = {}
            for name, values in self._series.items():
                if not values:
                    continue
                ordered = sorted(values)
                out[name] = {"last": round(values[-1]), "p50": round(ordered[len(ordered) // 2]),
                             "max": round(ordered[-1]), "n": len(values)}
            return out
