"""
memory_manager.py - Persistent memory for Alpha AI (SQLite, stdlib only).

Stores:
  * projects  - named project folders ("where is my python project?")
  * notes     - free-form user notes
  * activity  - foreground-window usage segments for screen-time summaries

One shared connection guarded by an RLock, so the activity-tracker thread,
the listener thread and the main thread can all use it safely.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("alpha.memory")

from config.settings import DB_PATH

DEFAULT_DB = DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    path       TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS activity (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    day      TEXT NOT NULL,
    app      TEXT NOT NULL,
    title    TEXT,
    start_ts REAL NOT NULL,
    end_ts   REAL NOT NULL,
    seconds  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_day ON activity(day);
CREATE TABLE IF NOT EXISTS messages (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   REAL NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    via  TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
"""

_NOISE_WORDS = re.compile(r"\b(my|the|a|an|project|projects|folder|directory)\b", re.I)


def _clean_name(name: str) -> str:
    cleaned = _NOISE_WORDS.sub(" ", name or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class MemoryManager:
    def __init__(self, db_path: Path | str = DEFAULT_DB, keep_activity_days: int = 90):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            log.warning("Could not enable WAL mode; continuing with defaults.")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self.purge_old_activity(keep_activity_days)
        self.purge_old_messages(30)

    # ------------------------------------------------------------------ core
    def _exec(self, sql: str, params: tuple = (), fetch: bool = False):
        with self._lock:
            try:
                cur = self._conn.execute(sql, params)
                rows = cur.fetchall() if fetch else None
                self._conn.commit()
                return rows if fetch else cur.lastrowid
            except sqlite3.Error:
                log.exception("SQLite error for: %s", sql)
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                pass

    # -------------------------------------------------------------- projects
    def save_project(self, name: str, path: str) -> None:
        name = _clean_name(name) or name.strip()
        if not name:
            raise ValueError("Project name is empty.")
        now = time.time()
        self._exec(
            """INSERT INTO projects (name, path, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET path=excluded.path, updated_at=excluded.updated_at""",
            (name, path, now, now),
        )

    def find_projects(self, query: str) -> List[Dict]:
        q = _clean_name(query) or query.strip()
        if not q:
            return []
        rows = self._exec("SELECT * FROM projects WHERE name = ?", (q,), fetch=True)
        if not rows:
            rows = self._exec(
                "SELECT * FROM projects WHERE name LIKE ? ESCAPE '\\' ORDER BY updated_at DESC",
                (f"%{_like_escape(q)}%",),
                fetch=True,
            )
        if not rows:  # every word of the query appears in the name
            tokens = [t for t in q.split() if t]
            if tokens:
                clause = " AND ".join(["name LIKE ? ESCAPE '\\'"] * len(tokens))
                rows = self._exec(
                    f"SELECT * FROM projects WHERE {clause} ORDER BY updated_at DESC",
                    tuple(f"%{_like_escape(t)}%" for t in tokens),
                    fetch=True,
                )
        return [self._project_dict(r) for r in rows]

    def list_projects(self) -> List[Dict]:
        rows = self._exec("SELECT * FROM projects ORDER BY name COLLATE NOCASE", fetch=True)
        return [self._project_dict(r) for r in rows]

    def delete_project(self, name: str) -> bool:
        with self._lock:
            before = self._conn.total_changes
            self._exec("DELETE FROM projects WHERE name = ?", (_clean_name(name) or name,))
            return self._conn.total_changes > before

    @staticmethod
    def _project_dict(row: sqlite3.Row) -> Dict:
        d = dict(row)
        d["exists"] = os.path.exists(d["path"])
        return d

    # ----------------------------------------------------------------- notes
    def add_note(self, text: str) -> int:
        return self._exec("INSERT INTO notes (text, created_at) VALUES (?, ?)", (text.strip(), time.time()))

    def get_notes(self, limit: int = 10) -> List[Dict]:
        rows = self._exec(
            "SELECT id, text, created_at FROM notes ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
            fetch=True,
        )
        return [dict(r) for r in rows]

    def search_notes(self, query: str, limit: int = 10) -> List[Dict]:
        rows = self._exec(
            "SELECT id, text, created_at FROM notes WHERE text LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT ?",
            (f"%{_like_escape(query)}%", int(limit)),
            fetch=True,
        )
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- activity
    def log_activity(self, app: str, title: str, start_ts: float, end_ts: float) -> None:
        seconds = end_ts - start_ts
        if seconds <= 0:
            return
        day = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d")
        self._exec(
            "INSERT INTO activity (day, app, title, start_ts, end_ts, seconds) VALUES (?,?,?,?,?,?)",
            (day, app, (title or "")[:200], start_ts, end_ts, seconds),
        )

    def activity_summary(self, start_day: str, end_day: str) -> Dict:
        """Return {'total': seconds, 'apps': [(app, seconds), ...]} for an inclusive day range."""
        rows = self._exec(
            """SELECT app, SUM(seconds) AS s FROM activity
               WHERE day BETWEEN ? AND ? GROUP BY app ORDER BY s DESC""",
            (start_day, end_day),
            fetch=True,
        )
        apps = [(r["app"], float(r["s"])) for r in rows]
        return {"total": sum(s for _, s in apps), "apps": apps}

    def purge_old_activity(self, keep_days: int) -> None:
        if keep_days <= 0:
            return
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        try:
            self._exec("DELETE FROM activity WHERE day < ?", (cutoff,))
        except sqlite3.Error:
            pass

    # ------------------------------------------------------- conversation log
    def log_message(self, role: str, text: str, via: str = "", ts: Optional[float] = None) -> None:
        self._exec("INSERT INTO messages (ts, role, text, via) VALUES (?,?,?,?)",
                   (ts or time.time(), role, text[:2000], via or ""))

    def recent_messages(self, limit: int = 40) -> List[Dict]:
        rows = self._exec("SELECT ts, role, text, via FROM messages ORDER BY ts DESC LIMIT ?",
                          (int(limit),), fetch=True)
        return [dict(r) for r in reversed(rows)]

    def purge_old_messages(self, keep_days: int) -> None:
        try:
            self._exec("DELETE FROM messages WHERE ts < ?", (time.time() - keep_days * 86400,))
        except sqlite3.Error:
            pass

    # ------------------------------------------------------ activity analytics
    def activity_daily(self, days: int = 7) -> List[tuple]:
        """[(YYYY-MM-DD, seconds), ...] for the last `days` days, oldest first, zero-filled."""
        today = datetime.now().date()
        first = today - timedelta(days=days - 1)
        rows = self._exec("SELECT day, SUM(seconds) AS s FROM activity WHERE day >= ? GROUP BY day",
                          (first.isoformat(),), fetch=True)
        totals = {r["day"]: float(r["s"]) for r in rows}
        return [((first + timedelta(days=i)).isoformat(), totals.get((first + timedelta(days=i)).isoformat(), 0.0))
                for i in range(days)]

    def activity_titles(self, start_day: str, end_day: str, limit: int = 200) -> List[tuple]:
        """[(app, window title, seconds)] heaviest first - used for productivity categories."""
        rows = self._exec(
            """SELECT app, title, SUM(seconds) AS s FROM activity WHERE day BETWEEN ? AND ?
               GROUP BY app, title ORDER BY s DESC LIMIT ?""", (start_day, end_day, int(limit)), fetch=True)
        return [(r["app"], r["title"] or "", float(r["s"])) for r in rows]
