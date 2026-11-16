"""A small SQLite cache shared by every data client.

Keys are strings; values are anything JSON can hold. Each entry remembers when it
was fetched and how long it is good for (its TTL, "time to live", in seconds). A
client asks the cache first. Past the TTL it goes to the network, and if the
network fails it can still serve the old value, marked stale, instead of nothing.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class CacheEntry:
    value: Any
    fetched_at: float   # unix seconds
    ttl: float          # seconds

    def age(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, now - self.fetched_at)

    def is_fresh(self, now: Optional[float] = None) -> bool:
        return self.age(now) <= self.ttl


class Cache:
    """Thread-safe enough for this app: one lock, one short-lived connection per call."""

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._run(
            "CREATE TABLE IF NOT EXISTS cache ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL,"
            " fetched_at REAL NOT NULL,"
            " ttl REAL NOT NULL)"
        )

    def _run(self, sql: str, params: tuple = (), fetch: bool = False):
        with self._lock:
            con = sqlite3.connect(self.path, timeout=10)
            try:
                cur = con.execute(sql, params)
                row = cur.fetchone() if fetch else None
                con.commit()
                return row
            finally:
                con.close()

    def now(self) -> float:
        """The cache's idea of the current unix time. Injectable so tests can age entries without waiting."""
        return float(self._clock())

    def get(self, key: str) -> Optional[CacheEntry]:
        row = self._run("SELECT value, fetched_at, ttl FROM cache WHERE key = ?", (key,), fetch=True)
        if row is None:
            return None
        return CacheEntry(value=json.loads(row[0]), fetched_at=row[1], ttl=row[2])

    def set(self, key: str, value: Any, ttl: float) -> None:
        payload = json.dumps(value)  # raises TypeError for values JSON cannot hold; that is a bug, surface it
        self._run(
            "INSERT OR REPLACE INTO cache (key, value, fetched_at, ttl) VALUES (?, ?, ?, ?)",
            (key, payload, float(self._clock()), float(ttl)),
        )

    def delete(self, key: str) -> None:
        self._run("DELETE FROM cache WHERE key = ?", (key,))

    def count(self) -> int:
        row = self._run("SELECT COUNT(*) FROM cache", fetch=True)
        return int(row[0]) if row else 0
