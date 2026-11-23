"""The news store: every headline once, and the price reactions scheduled for it.

Lives in the same SQLite file as the cache (data/terminal.db), in two tables:
`news` (one row per headline or filing, de-duplicated on the way in) and
`reactions` (one row per headline per watchlist ticker it relates to, with the
price at the headline and 1 hour, 1 day and 5 days later, filled in as time passes).
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from ..datapoint import utcnow
from .dedupe import find_original, story_key
from .models import NewsItem, Reaction, iso, parse_when

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS news (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        headline TEXT NOT NULL,
        summary TEXT,
        url TEXT,
        published_at TEXT NOT NULL,
        symbols TEXT NOT NULL,
        catalyst TEXT NOT NULL,
        sentiment REAL,
        sentiment_note TEXT,
        story_key TEXT,
        duplicate_of TEXT,
        first_seen TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS news_published ON news(published_at)",
    """
    CREATE TABLE IF NOT EXISTS reactions (
        news_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        relation TEXT NOT NULL,
        catalyst TEXT NOT NULL,
        published_at TEXT NOT NULL,
        p0 REAL, p0_at TEXT,
        p1h REAL, p1h_at TEXT,
        p1d REAL, p1d_at TEXT,
        p5d REAL, p5d_at TEXT,
        status TEXT NOT NULL,
        note TEXT,
        next_try_at TEXT,
        updated_at TEXT,
        PRIMARY KEY (news_id, symbol)
    )
    """,
    "CREATE INDEX IF NOT EXISTS reactions_due ON reactions(status, next_try_at)",
)


class NewsStore:
    def __init__(self, path: str | Path, dedupe_hours: float = 48.0, dedupe_similarity: float = 0.5) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dedupe_hours = dedupe_hours
        self.dedupe_similarity = dedupe_similarity
        self._lock = threading.Lock()
        with self._connect() as con:
            for statement in SCHEMA:
                con.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    # ----- news -----
    def has(self, item_id: str) -> bool:
        with self._lock, self._connect() as con:
            return con.execute("SELECT 1 FROM news WHERE id = ?", (item_id,)).fetchone() is not None

    def get(self, item_id: str) -> Optional[NewsItem]:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM news WHERE id = ?", (item_id,)).fetchone()
        return NewsItem.from_row(row) if row else None

    def recent(self, since: datetime, until: Optional[datetime] = None) -> list[NewsItem]:
        """Every item published in the window, newest first, duplicates included (callers filter)."""
        params: list[Any] = [iso(since)]
        sql = "SELECT * FROM news WHERE published_at >= ?"
        if until is not None:
            sql += " AND published_at <= ?"
            params.append(iso(until))
        sql += " ORDER BY published_at DESC"
        with self._lock, self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [NewsItem.from_row(r) for r in rows]

    def upsert_items(self, items: Iterable[NewsItem], now: Optional[datetime] = None) -> tuple[list[NewsItem], int]:
        """Store items that are new. Returns (the new originals, how many were duplicates of a known story)."""
        now = now or utcnow()
        new_originals: list[NewsItem] = []
        duplicates = 0
        with self._lock, self._connect() as con:
            for item in items:
                exists = con.execute("SELECT 1 FROM news WHERE id = ?", (item.id,)).fetchone()
                if exists:
                    continue
                window_start = iso(item.published_at - timedelta(hours=self.dedupe_hours))
                window_end = iso(item.published_at + timedelta(hours=self.dedupe_hours))
                rows = con.execute(
                    "SELECT * FROM news WHERE published_at BETWEEN ? AND ? AND duplicate_of IS NULL",
                    (window_start, window_end),
                ).fetchall()
                original = find_original(item, (NewsItem.from_row(r) for r in rows), self.dedupe_hours, self.dedupe_similarity)
                item.first_seen = item.first_seen or now
                if original is not None:
                    item.duplicate_of = original.id
                    duplicates += 1
                row = item.to_row()
                row["story_key"] = story_key(item.headline)
                con.execute(
                    "INSERT INTO news (id, source, kind, headline, summary, url, published_at, symbols, catalyst,"
                    " sentiment, sentiment_note, story_key, duplicate_of, first_seen)"
                    " VALUES (:id, :source, :kind, :headline, :summary, :url, :published_at, :symbols, :catalyst,"
                    " :sentiment, :sentiment_note, :story_key, :duplicate_of, :first_seen)",
                    row,
                )
                if original is None:
                    new_originals.append(item)
        return new_originals, duplicates

    def unscored(self, since: datetime, limit: int = 200) -> list[NewsItem]:
        """Originals with no sentiment yet (duplicates borrow the original's score at display time)."""
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM news WHERE sentiment IS NULL AND duplicate_of IS NULL AND published_at >= ?"
                " ORDER BY published_at DESC LIMIT ?",
                (iso(since), limit),
            ).fetchall()
        return [NewsItem.from_row(r) for r in rows]

    def set_sentiment(self, item_id: str, score: Optional[float], note: str) -> None:
        with self._lock, self._connect() as con:
            con.execute("UPDATE news SET sentiment = ?, sentiment_note = ? WHERE id = ?", (score, note, item_id))

    def count(self) -> int:
        with self._lock, self._connect() as con:
            return int(con.execute("SELECT COUNT(*) FROM news").fetchone()[0])

    # ----- reactions -----
    def schedule_reaction(self, item: NewsItem, symbol: str, relation: str) -> bool:
        """Create the row for (headline, ticker) unless it exists. Returns True when created."""
        first_due = item.published_at + timedelta(hours=1)
        with self._lock, self._connect() as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO reactions (news_id, symbol, relation, catalyst, published_at, status, note, next_try_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', '', ?, ?)",
                (item.id, symbol.upper(), relation, item.catalyst, iso(item.published_at), iso(first_due), iso(utcnow())),
            )
            return cur.rowcount == 1

    def due_reactions(self, now: Optional[datetime] = None, limit: int = 20) -> list[Reaction]:
        now = now or utcnow()
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM reactions WHERE status = 'pending' AND (next_try_at IS NULL OR next_try_at <= ?)"
                " ORDER BY published_at ASC LIMIT ?",
                (iso(now), limit),
            ).fetchall()
        return [self._reaction_from_row(r) for r in rows]

    def save_reaction(self, reaction: Reaction) -> None:
        reaction.updated_at = utcnow()
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE reactions SET p0 = ?, p0_at = ?, p1h = ?, p1h_at = ?, p1d = ?, p1d_at = ?, p5d = ?, p5d_at = ?,"
                " status = ?, note = ?, next_try_at = ?, updated_at = ? WHERE news_id = ? AND symbol = ?",
                (
                    reaction.p0, iso(reaction.p0_at), reaction.p1h, iso(reaction.p1h_at),
                    reaction.p1d, iso(reaction.p1d_at), reaction.p5d, iso(reaction.p5d_at),
                    reaction.status, reaction.note, iso(reaction.next_try_at), iso(reaction.updated_at),
                    reaction.news_id, reaction.symbol.upper(),
                ),
            )

    def reactions_for(self, news_ids: Iterable[str], symbol: str) -> dict[str, Reaction]:
        ids = list(news_ids)
        if not ids:
            return {}
        out: dict[str, Reaction] = {}
        with self._lock, self._connect() as con:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                rows = con.execute(
                    f"SELECT * FROM reactions WHERE symbol = ? AND news_id IN ({marks})", [symbol.upper(), *chunk]
                ).fetchall()
                for r in rows:
                    out[r["news_id"]] = self._reaction_from_row(r)
        return out

    def reaction_summary(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        """Average percent move after headlines, grouped by ticker, relation and catalyst. Only filled slots count."""
        sql = (
            "SELECT symbol, relation, catalyst, COUNT(*) AS n,"
            " AVG(CASE WHEN p0 > 0 AND p1h IS NOT NULL THEN (p1h / p0 - 1.0) * 100.0 END) AS avg_1h,"
            " SUM(CASE WHEN p0 > 0 AND p1h IS NOT NULL THEN 1 ELSE 0 END) AS n_1h,"
            " AVG(CASE WHEN p0 > 0 AND p1d IS NOT NULL THEN (p1d / p0 - 1.0) * 100.0 END) AS avg_1d,"
            " SUM(CASE WHEN p0 > 0 AND p1d IS NOT NULL THEN 1 ELSE 0 END) AS n_1d,"
            " AVG(CASE WHEN p0 > 0 AND p5d IS NOT NULL THEN (p5d / p0 - 1.0) * 100.0 END) AS avg_5d,"
            " SUM(CASE WHEN p0 > 0 AND p5d IS NOT NULL THEN 1 ELSE 0 END) AS n_5d"
            " FROM reactions"
        )
        params: list[Any] = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol.upper())
        sql += " GROUP BY symbol, relation, catalyst ORDER BY symbol, relation, catalyst"
        with self._lock, self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def reaction_counts(self) -> dict[str, int]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT status, COUNT(*) AS n FROM reactions GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    @staticmethod
    def _reaction_from_row(row: sqlite3.Row) -> Reaction:
        return Reaction(
            news_id=row["news_id"],
            symbol=row["symbol"],
            relation=row["relation"],
            catalyst=row["catalyst"],
            published_at=parse_when(row["published_at"]),
            p0=row["p0"], p0_at=parse_when(row["p0_at"]),
            p1h=row["p1h"], p1h_at=parse_when(row["p1h_at"]),
            p1d=row["p1d"], p1d_at=parse_when(row["p1d_at"]),
            p5d=row["p5d"], p5d_at=parse_when(row["p5d_at"]),
            status=row["status"],
            note=row["note"] or "",
            next_try_at=parse_when(row["next_try_at"]),
            updated_at=parse_when(row["updated_at"]),
        )
