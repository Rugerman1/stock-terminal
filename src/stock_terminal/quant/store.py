"""Market data kept for the quant read: daily closes, every score run, and the signal log.

Same SQLite file as the cache and the news (data/terminal.db). Daily bars accumulate
forward so a year of history is pulled once and topped up; scores are kept per run so
the backtest and the validity engine can look back; the signal log records a label
only when it changes (one open long-term signal per ticker), which is design call 1 in
the project note.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from ..datapoint import utcnow
from ..news.models import iso, parse_when

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS daily_bars (
        symbol TEXT NOT NULL,
        date TEXT NOT NULL,
        close REAL NOT NULL,
        PRIMARY KEY (symbol, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scores (
        run_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        as_of TEXT,
        composite REAL,
        label TEXT,
        percentile REAL,
        factors TEXT NOT NULL,
        PRIMARY KEY (run_at, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        kind TEXT NOT NULL,
        label TEXT NOT NULL,
        direction INTEGER NOT NULL,
        opened_at TEXT NOT NULL,
        open_price REAL,
        horizon_days INTEGER NOT NULL,
        closed_at TEXT,
        close_price REAL,
        outcome INTEGER,
        note TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS signals_open ON signals(ticker, kind, closed_at)",
    """
    CREATE TABLE IF NOT EXISTS iv_history (
        symbol TEXT NOT NULL,
        date TEXT NOT NULL,
        iv REAL NOT NULL,
        contract TEXT,
        PRIMARY KEY (symbol, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT NOT NULL,
        params TEXT NOT NULL,
        params_hash TEXT NOT NULL,
        result TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experiments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        params_hash TEXT NOT NULL UNIQUE,
        params TEXT NOT NULL,
        sharpe REAL,
        n_returns INTEGER,
        skew REAL,
        kurtosis REAL,
        note TEXT
    )
    """,
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
)

LONG_TERM = "long_term"
OPTIONS = "options"
DIRECTIONS = {"Favorable": 1, "Unfavorable": -1, "Neutral": 0, "Call candidate": 1, "Put candidate": -1, "No trade": 0}


@dataclass
class Signal:
    id: int
    ticker: str
    kind: str
    label: str
    direction: int
    opened_at: datetime
    open_price: Optional[float]
    horizon_days: int
    closed_at: Optional[datetime] = None
    close_price: Optional[float] = None
    outcome: Optional[int] = None
    note: str = ""
    target: Optional[float] = None          # options: the breakeven at the time
    scored_at: Optional[datetime] = None
    score_price: Optional[float] = None
    stock_return: Optional[float] = None
    bench_return: Optional[float] = None
    score_note: str = ""


class MarketStore:
    # Columns added after the table was first created; ALTER TABLE adds them to an existing database.
    SIGNAL_COLUMNS = (("target", "REAL"), ("scored_at", "TEXT"), ("score_price", "REAL"), ("stock_return", "REAL"), ("bench_return", "REAL"), ("score_note", "TEXT"))

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            for statement in SCHEMA:
                con.execute(statement)
            present = {row["name"] for row in con.execute("PRAGMA table_info(signals)").fetchall()}
            for name, kind in self.SIGNAL_COLUMNS:
                if name not in present:
                    con.execute(f"ALTER TABLE signals ADD COLUMN {name} {kind}")
            bars = {row["name"] for row in con.execute("PRAGMA table_info(daily_bars)").fetchall()}
            for name in ("high", "low"):
                if name not in bars:
                    con.execute(f"ALTER TABLE daily_bars ADD COLUMN {name} REAL")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    # ----- daily bars -----
    def upsert_bars(self, symbol: str, rows: Iterable[tuple]) -> int:
        """rows: (date 'YYYY-MM-DD', close) or (date, close, high, low). Returns how many rows were written."""
        data = []
        for row in rows:
            d, c = row[0], float(row[1])
            high = float(row[2]) if len(row) > 2 and row[2] is not None else None
            low = float(row[3]) if len(row) > 3 and row[3] is not None else None
            data.append((symbol.upper(), d, c, high, low))
        if not data:
            return 0
        with self._lock, self._connect() as con:
            con.executemany("INSERT OR REPLACE INTO daily_bars (symbol, date, close, high, low) VALUES (?, ?, ?, ?, ?)", data)
        return len(data)

    def has_ranges(self, symbol: str) -> bool:
        """True when the newest stored bar carries a high and a low (needed for the ATR in the sizing panel)."""
        with self._lock, self._connect() as con:
            row = con.execute("SELECT high, low FROM daily_bars WHERE symbol = ? ORDER BY date DESC LIMIT 1", (symbol.upper(),)).fetchone()
        return bool(row and row["high"] is not None and row["low"] is not None)

    def bars(self, symbol: str, limit: int = 400) -> list[tuple[str, float, Optional[float], Optional[float]]]:
        """(date, close, high, low), oldest first."""
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT date, close, high, low FROM daily_bars WHERE symbol = ? ORDER BY date DESC LIMIT ?", (symbol.upper(), limit)
            ).fetchall()
        return [(r["date"], float(r["close"]), r["high"], r["low"]) for r in reversed(rows)]

    def first_bar_date(self, symbol: str) -> Optional[str]:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT MIN(date) FROM daily_bars WHERE symbol = ?", (symbol.upper(),)).fetchone()
        return row[0] if row and row[0] else None

    def last_bar_date(self, symbol: str) -> Optional[str]:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT MAX(date) FROM daily_bars WHERE symbol = ?", (symbol.upper(),)).fetchone()
        return row[0] if row and row[0] else None

    def closes(self, symbol: str, limit: int = 400) -> list[tuple[str, float]]:
        """The newest `limit` closes for a symbol, oldest first."""
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT date, close FROM daily_bars WHERE symbol = ? ORDER BY date DESC LIMIT ?", (symbol.upper(), limit)
            ).fetchall()
        return [(r["date"], float(r["close"])) for r in reversed(rows)]

    def closes_many(self, symbols: Iterable[str], limit: int = 400) -> dict[str, list[tuple[str, float]]]:
        return {s.upper(): self.closes(s, limit) for s in symbols}

    def bar_counts(self) -> dict[str, int]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT symbol, COUNT(*) AS n FROM daily_bars GROUP BY symbol").fetchall()
        return {r["symbol"]: int(r["n"]) for r in rows}

    # ----- scores -----
    def save_scores(self, run_at: datetime, rows: Iterable[dict[str, Any]]) -> None:
        """rows: {symbol, as_of, composite, label, percentile, factors (JSON-able)}."""
        stamp = iso(run_at)
        data = [
            (stamp, r["symbol"].upper(), r.get("as_of"), r.get("composite"), r.get("label"), r.get("percentile"), json.dumps(r.get("factors") or {}))
            for r in rows
        ]
        with self._lock, self._connect() as con:
            con.executemany(
                "INSERT OR REPLACE INTO scores (run_at, symbol, as_of, composite, label, percentile, factors) VALUES (?, ?, ?, ?, ?, ?, ?)",
                data,
            )

    def latest_run(self) -> Optional[datetime]:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT MAX(run_at) FROM scores").fetchone()
        return parse_when(row[0]) if row and row[0] else None

    def latest_scores(self) -> dict[str, dict[str, Any]]:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT MAX(run_at) FROM scores").fetchone()
            if not row or not row[0]:
                return {}
            rows = con.execute("SELECT * FROM scores WHERE run_at = ?", (row[0],)).fetchall()
        out = {}
        for r in rows:
            out[r["symbol"]] = {
                "run_at": r["run_at"], "as_of": r["as_of"], "composite": r["composite"], "label": r["label"],
                "percentile": r["percentile"], "factors": json.loads(r["factors"] or "{}"),
            }
        return out

    def score_runs(self, symbol: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT run_at, as_of, composite, label FROM scores WHERE symbol = ? ORDER BY run_at", (symbol.upper(),)).fetchall()
        return [dict(r) for r in rows]

    # ----- signals -----
    def open_signal(self, ticker: str, kind: str = LONG_TERM) -> Optional[Signal]:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM signals WHERE ticker = ? AND kind = ? AND closed_at IS NULL ORDER BY id DESC LIMIT 1",
                (ticker.upper(), kind),
            ).fetchone()
        return self._signal(row) if row else None

    def record_label(
        self, ticker: str, label: str, price: Optional[float], at: datetime, horizon_days: int = 63, kind: str = LONG_TERM, detail: str = "",
        target: Optional[float] = None,
    ) -> Optional[Signal]:
        """Open a new signal when the label (or its detail, the contract for options) differs from the open one, closing
        the old. Returns the new signal, or None when nothing changed."""
        ticker = ticker.upper()
        current = self.open_signal(ticker, kind)
        if current is not None and current.label == label and (not detail or current.note == detail):
            return None
        with self._lock, self._connect() as con:
            if current is not None:
                con.execute(
                    "UPDATE signals SET closed_at = ?, close_price = ?, note = ? WHERE id = ?",
                    (iso(at), price, (current.note + "; " if current.note else "") + f"label changed to {label}", current.id),
                )
            cur = con.execute(
                "INSERT INTO signals (ticker, kind, label, direction, opened_at, open_price, horizon_days, note, target) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticker, kind, label, DIRECTIONS.get(label, 0), iso(at), price, horizon_days, detail, target),
            )
            new_id = cur.lastrowid
            row = con.execute("SELECT * FROM signals WHERE id = ?", (new_id,)).fetchone()
        return self._signal(row)

    def close_open(self, ticker: str, kind: str, price: Optional[float], at: datetime, reason: str) -> Optional[Signal]:
        """Close the open signal of this kind, if any (an options candidate that turned into No trade)."""
        current = self.open_signal(ticker, kind)
        if current is None:
            return None
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE signals SET closed_at = ?, close_price = ?, note = ? WHERE id = ?",
                (iso(at), price, (current.note + "; " if current.note else "") + reason, current.id),
            )
            row = con.execute("SELECT * FROM signals WHERE id = ?", (current.id,)).fetchone()
        return self._signal(row)

    # ----- implied volatility history (one at-the-money IV per symbol per day, for IV rank) -----
    def record_iv(self, symbol: str, day: str, iv: float, contract: str = "") -> None:
        with self._lock, self._connect() as con:
            con.execute("INSERT OR REPLACE INTO iv_history (symbol, date, iv, contract) VALUES (?, ?, ?, ?)", (symbol.upper(), day, float(iv), contract))

    def iv_rank(self, symbol: str, iv_now: Optional[float], window: int = 252, need: int = 252) -> tuple[Optional[float], int]:
        """(rank between 0 and 1, days stored). The rank is None until `need` days are stored: the spec says collecting, n/252."""
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT iv FROM iv_history WHERE symbol = ? ORDER BY date DESC LIMIT ?", (symbol.upper(), window)).fetchall()
        values = [float(r["iv"]) for r in rows]
        n = len(values)
        if iv_now is None or n < need:
            return None, n
        lo, hi = min(values), max(values)
        if hi <= lo:
            return None, n
        return max(0.0, min(1.0, (iv_now - lo) / (hi - lo))), n

    def unscored_signals(self, kind: Optional[str] = None) -> list[Signal]:
        sql, params = "SELECT * FROM signals WHERE scored_at IS NULL", []
        if kind:
            sql += " AND kind = ?"; params.append(kind)
        with self._lock, self._connect() as con:
            rows = con.execute(sql + " ORDER BY id", params).fetchall()
        return [self._signal(r) for r in rows]

    def scored_signals(self, kind: str) -> list[Signal]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT * FROM signals WHERE kind = ? AND outcome IS NOT NULL ORDER BY scored_at, id", (kind,)).fetchall()
        return [self._signal(r) for r in rows]

    def open_count(self, kind: str) -> int:
        with self._lock, self._connect() as con:
            return int(con.execute("SELECT COUNT(*) FROM signals WHERE kind = ? AND closed_at IS NULL AND scored_at IS NULL AND direction != 0", (kind,)).fetchone()[0])

    def save_outcome(self, signal_id: int, outcome: Optional[int], scored_at: datetime, score_price: Optional[float], stock_return: Optional[float], bench_return: Optional[float], note: str) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE signals SET outcome = ?, scored_at = ?, score_price = ?, stock_return = ?, bench_return = ?, score_note = ? WHERE id = ?",
                (outcome, iso(scored_at), score_price, stock_return, bench_return, note, signal_id),
            )

    def signals(self, ticker: Optional[str] = None, kind: Optional[str] = None) -> list[Signal]:
        sql, params = "SELECT * FROM signals", []
        clauses = []
        if ticker:
            clauses.append("ticker = ?"); params.append(ticker.upper())
        if kind:
            clauses.append("kind = ?"); params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock, self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [self._signal(r) for r in rows]

    # ----- backtests, experiments, settings -----
    def save_backtest(self, run_at: datetime, params: dict[str, Any], params_hash: str, result: dict[str, Any]) -> int:
        with self._lock, self._connect() as con:
            cur = con.execute("INSERT INTO backtests (run_at, params, params_hash, result) VALUES (?, ?, ?, ?)", (iso(run_at), json.dumps(params, sort_keys=True), params_hash, json.dumps(result)))
            return int(cur.lastrowid)

    def latest_backtest(self) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM backtests ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"id": row["id"], "run_at": parse_when(row["run_at"]), "params": json.loads(row["params"]), "params_hash": row["params_hash"], "result": json.loads(row["result"])}

    def record_experiment(self, at: datetime, params_hash: str, params: dict[str, Any], sharpe: Optional[float], n_returns: int, skew: Optional[float], kurtosis: Optional[float], note: str = "") -> bool:
        """One row per distinct parameter set. Returns True when this set is new (the trial counter went up)."""
        with self._lock, self._connect() as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO experiments (at, params_hash, params, sharpe, n_returns, skew, kurtosis, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (iso(at), params_hash, json.dumps(params, sort_keys=True), sharpe, n_returns, skew, kurtosis, note),
            )
            if cur.rowcount == 0:
                con.execute("UPDATE experiments SET sharpe = ?, n_returns = ?, skew = ?, kurtosis = ? WHERE params_hash = ?", (sharpe, n_returns, skew, kurtosis, params_hash))
                return False
            return True

    def experiments(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT * FROM experiments ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connect() as con:
            con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

    @staticmethod
    def _signal(row: sqlite3.Row) -> Signal:
        return Signal(
            id=int(row["id"]), ticker=row["ticker"], kind=row["kind"], label=row["label"], direction=int(row["direction"]),
            opened_at=parse_when(row["opened_at"]), open_price=row["open_price"], horizon_days=int(row["horizon_days"]),
            closed_at=parse_when(row["closed_at"]), close_price=row["close_price"], outcome=row["outcome"], note=row["note"] or "",
            target=row["target"], scored_at=parse_when(row["scored_at"]), score_price=row["score_price"],
            stock_return=row["stock_return"], bench_return=row["bench_return"], score_note=row["score_note"] or "",
        )
