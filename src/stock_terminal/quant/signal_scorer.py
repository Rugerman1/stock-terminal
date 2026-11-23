"""Scoring logged signals against stored daily closes, so the validity tests have outcomes to count.

A signal's window runs from the session it was logged on (the close that was the
price then) to its horizon: the 63rd session after it for a long-term label, the
contract's expiry for an options candidate, or the session the label changed on when
that came first. Long-term: 1 when the stock beat SPY in the predicted direction over
the window. Options: 1 when the stock ended beyond the breakeven on the predicted side.
Neutral labels predict nothing and are marked skipped. Every universe name gets the
same treatment over the same window so the tests have a baseline and a random pool.
Prices come from the stored closes only; nothing is estimated between sessions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from .store import LONG_TERM, OPTIONS, MarketStore, Signal
from .validity import ScoredSignal

log = logging.getLogger("stock_terminal.validity")
BENCHMARK = "SPY"


@dataclass
class Window:
    start_date: str          # session date of the entry close
    end_date: str            # session date of the exit close
    reason: str              # "horizon", "closed early"


def session_on_or_before(closes: list[tuple[str, float]], day: date) -> Optional[tuple[str, float]]:
    """The last stored close on or before the date."""
    iso = day.isoformat()
    best = None
    for d, c in closes:
        if d <= iso:
            best = (d, c)
        else:
            break
    return best


def session_n_after(closes: list[tuple[str, float]], day: date, n: int) -> Optional[tuple[str, float]]:
    """The n-th stored close strictly after the date (n = 63 for the long-term horizon)."""
    iso = day.isoformat()
    later = [(d, c) for d, c in closes if d > iso]
    return later[n - 1] if len(later) >= n else None


def window_for(signal: Signal, closes: list[tuple[str, float]]) -> Optional[Window]:
    """Where the signal's window ends, or None while the market has not got there yet."""
    start = session_on_or_before(closes, signal.opened_at.date())
    if start is None:
        return None
    if signal.closed_at is not None:
        end = session_on_or_before(closes, signal.closed_at.date())
        if end is not None and end[0] > start[0]:
            # Closed early: the label changed before the horizon, which is the system closing the bet.
            horizon_end = _horizon_close(signal, closes)
            if horizon_end is None or end[0] <= horizon_end[0]:
                return Window(start[0], end[0], "closed early")
    end = _horizon_close(signal, closes)
    if end is None:
        return None
    return Window(start[0], end[0], "horizon")


def _horizon_close(signal: Signal, closes: list[tuple[str, float]]) -> Optional[tuple[str, float]]:
    if signal.kind == OPTIONS:
        expiry = signal.opened_at.date()
        from datetime import timedelta
        expiry = expiry + timedelta(days=signal.horizon_days)
        last = closes[-1][0] if closes else None
        if last is None or last < expiry.isoformat():
            return None
        return session_on_or_before(closes, expiry)
    return session_n_after(closes, signal.opened_at.date(), signal.horizon_days)


def close_on(closes: list[tuple[str, float]], session: str) -> Optional[float]:
    for d, c in closes:
        if d == session:
            return c
    return None


def outcome_for(kind: str, direction: int, start_price: float, end_price: float, bench_start: Optional[float], bench_end: Optional[float], target: Optional[float]) -> tuple[Optional[int], float, Optional[float], str]:
    """(outcome, stock return, benchmark return, note). None outcome when it cannot be judged."""
    if start_price <= 0 or end_price <= 0:
        return None, 0.0, None, "price not positive"
    stock_ret = end_price / start_price - 1.0
    if kind == OPTIONS:
        if target is not None and target > 0:
            return int(direction * (end_price - target) > 0), stock_ret, None, f"vs breakeven {target:.2f}"
        return int(direction * stock_ret > 0), stock_ret, None, "no breakeven stored; raw direction"
    if bench_start is None or bench_end is None or bench_start <= 0:
        return None, stock_ret, None, f"no {BENCHMARK} close for the window"
    bench_ret = bench_end / bench_start - 1.0
    return int(direction * (stock_ret - bench_ret) > 0), stock_ret, bench_ret, f"vs {BENCHMARK}"


class SignalScorer:
    def __init__(self, store: MarketStore, universe: list[str], history_days: int = 400, as_of: Optional[date] = None) -> None:
        self.store = store
        self.universe = [u for u in universe if u != BENCHMARK]
        self.history_days = history_days
        self.as_of = as_of            # closes after this date are not visible (the backtest and the tests rely on it)
        self._closes: dict[str, list[tuple[str, float]]] = {}

    def closes(self, symbol: str) -> list[tuple[str, float]]:
        symbol = symbol.upper()
        if symbol not in self._closes:
            rows = self.store.closes(symbol, self.history_days)
            if self.as_of is not None:
                limit = self.as_of.isoformat()
                rows = [(d, c) for d, c in rows if d <= limit]
            self._closes[symbol] = rows
        return self._closes[symbol]

    def score_due(self, now: Optional[datetime] = None) -> list[str]:
        """Score every signal whose window has ended by `now`. Returns one line per signal scored."""
        now = now or datetime.now(timezone.utc)
        if self.as_of != now.date():
            self.as_of = now.date()
            self._closes = {}
        events: list[str] = []
        for signal in self.store.unscored_signals():
            if signal.direction == 0:
                self.store.save_outcome(signal.id, None, now, None, None, None, "Neutral: nothing to score")
                continue
            closes = self.closes(signal.ticker)
            window = window_for(signal, closes)
            if window is None:
                continue
            start_price, end_price = close_on(closes, window.start_date), close_on(closes, window.end_date)
            if start_price is None or end_price is None:
                continue
            spy = self.closes(BENCHMARK)
            outcome, stock_ret, bench_ret, note = outcome_for(
                signal.kind, signal.direction, start_price, end_price, close_on(spy, window.start_date), close_on(spy, window.end_date), signal.target
            )
            if outcome is None:
                log.warning("signal %s %s cannot be scored: %s", signal.ticker, signal.id, note)
                continue
            scored_at = datetime.fromisoformat(window.end_date).replace(tzinfo=timezone.utc)
            self.store.save_outcome(signal.id, outcome, scored_at, end_price, stock_ret, bench_ret, f"{window.reason}; {note}")
            events.append(f"{signal.ticker} {signal.kind} {signal.label} {window.start_date} to {window.end_date}: {'win' if outcome else 'loss'} ({window.reason}, {note})")
            log.info("scored signal %s: %s", signal.id, events[-1])
        return events

    def universe_outcomes(self, kind: str, direction: int, start_date: str, end_date: str, target_ratio: Optional[float] = None) -> list[int]:
        """Every universe name's outcome over the same window: beat SPY in the direction (long-term) or moved that way (raw)."""
        spy = self.closes(BENCHMARK)
        spy_start, spy_end = close_on(spy, start_date), close_on(spy, end_date)
        out: list[int] = []
        for name in self.universe:
            closes = self.closes(name)
            s, e = close_on(closes, start_date), close_on(closes, end_date)
            if s is None or e is None or s <= 0:
                continue
            if kind == OPTIONS:
                # A random name's "breakeven" at the same relative distance as the real contract's, when known.
                target = s * target_ratio if target_ratio else None
                outcome, _, _, _ = outcome_for(OPTIONS, direction, s, e, None, None, target)
            else:
                outcome, _, _, _ = outcome_for(LONG_TERM, direction, s, e, spy_start, spy_end, None)
            if outcome is not None:
                out.append(outcome)
        return out

    def scored(self, kind: str) -> list[ScoredSignal]:
        out: list[ScoredSignal] = []
        for signal in self.store.scored_signals(kind):
            if signal.outcome is None or signal.scored_at is None:
                continue
            closes = self.closes(signal.ticker)
            start = session_on_or_before(closes, signal.opened_at.date())
            start_date = start[0] if start else signal.opened_at.date().isoformat()
            end_date = signal.scored_at.date().isoformat()
            ratio = (signal.target / start[1]) if (kind == OPTIONS and signal.target and start and start[1] > 0) else None
            out.append(ScoredSignal(
                ticker=signal.ticker, direction=signal.direction, outcome=int(signal.outcome),
                start=signal.opened_at, end=signal.scored_at,
                universe_outcomes=self.universe_outcomes(kind, signal.direction, start_date, end_date, ratio),
            ))
        return out

    def gap_outcome(self, ticker: str, direction: int, start: datetime, end: datetime) -> Optional[int]:
        """For the BEF's inserted test trade: did the market move the test trade's way (vs SPY) over the gap?"""
        closes = self.closes(ticker)
        a = session_on_or_before(closes, start.date())
        b = session_on_or_before(closes, end.date())
        if a is None or b is None or a[0] >= b[0]:
            return None
        spy = self.closes(BENCHMARK)
        outcome, _, _, _ = outcome_for(LONG_TERM, direction, a[1], b[1], close_on(spy, a[0]), close_on(spy, b[0]), None)
        return outcome
