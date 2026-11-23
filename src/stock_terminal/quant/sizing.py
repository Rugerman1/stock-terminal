"""Position sizing (book p. 65) and the watchlist correlation table. Display only: nothing here touches a signal.

The book's rule: risk management is only the choice of how much capital to put in, and
it must never change the signal. So this is a separate panel. Given an account size:
shares such that a stop 2 x ATR away loses 1 percent of the account, and for options
the number of contracts whose total premium is 1 percent of the account or less. The
ATR is the simple mean of the true range over the last 14 sessions. The correlation
table uses 90 daily log returns and warns when two holdings sit above 0.8.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

Bar = tuple[str, float, Optional[float], Optional[float]]   # date, close, high, low


def true_ranges(bars: list[Bar]) -> list[float]:
    """True range per session: max(high - low, |high - previous close|, |low - previous close|). Needs highs and lows."""
    out: list[float] = []
    for i in range(1, len(bars)):
        _, prev_close, _, _ = bars[i - 1]
        _, close, high, low = bars[i]
        if high is None or low is None:
            continue
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def atr(bars: list[Bar], sessions: int = 14) -> Optional[float]:
    ranges = true_ranges(bars)
    if len(ranges) < sessions:
        return None
    return float(sum(ranges[-sessions:]) / sessions)


def shares_for(account: float, risk_pct: float, atr_value: Optional[float], multiple: float) -> Optional[int]:
    """Shares such that a stop `multiple` ATRs away loses `risk_pct` percent of the account."""
    if atr_value is None or atr_value <= 0 or account <= 0:
        return None
    return int(math.floor(account * risk_pct / 100.0 / (multiple * atr_value)))


def contracts_for(account: float, risk_pct: float, ask: Optional[float]) -> Optional[int]:
    """Contracts whose total premium (ask x 100 each) stays within `risk_pct` percent of the account."""
    if ask is None or ask <= 0 or account <= 0:
        return None
    return int(math.floor(account * risk_pct / 100.0 / (ask * 100.0)))


@dataclass
class SizingRow:
    ticker: str
    price: Optional[float]
    atr: Optional[float]
    stop_distance: Optional[float]
    shares: Optional[int]
    ask: Optional[float]
    contracts: Optional[int]
    note: str = ""


@dataclass
class Correlation:
    tickers: list[str]
    matrix: dict[tuple[str, str], float] = field(default_factory=dict)
    sessions: int = 0
    warnings: list[str] = field(default_factory=list)
    note: str = ""


def correlation(closes_by_symbol: dict[str, list[tuple[str, float]]], sessions: int = 90, warn_above: float = 0.8) -> Correlation:
    """Pearson correlation of daily log returns over the last `sessions` sessions the names have in common."""
    tickers = [t for t, rows in closes_by_symbol.items() if len(rows) >= 2]
    corr = Correlation(tickers=tickers)
    if len(tickers) < 2:
        corr.note = "needs two or more tickers with closes"
        return corr
    common = set.intersection(*(set(d for d, _ in closes_by_symbol[t]) for t in tickers))
    dates = sorted(common)[-(sessions + 1):]
    if len(dates) < 20:
        corr.note = f"only {max(len(dates) - 1, 0)} shared sessions; needs 20"
        return corr
    returns = []
    for t in tickers:
        at = dict(closes_by_symbol[t])
        series = [at[d] for d in dates]
        returns.append([math.log(series[i] / series[i - 1]) for i in range(1, len(series))])
    with np.errstate(invalid="ignore", divide="ignore"):
        matrix = np.corrcoef(np.asarray(returns))
    flat = [t for t, r in zip(tickers, returns) if float(np.std(r)) == 0.0]
    if flat:
        corr.note = "no price variation over the window: " + ", ".join(flat)
    matrix = np.nan_to_num(matrix, nan=0.0)
    corr.sessions = len(dates) - 1
    for i, a in enumerate(tickers):
        for j, b in enumerate(tickers):
            corr.matrix[(a, b)] = float(matrix[i, j])
            if i < j and matrix[i, j] > warn_above:
                corr.warnings.append(f"{a} and {b} move together: correlation {matrix[i, j]:.2f} over {corr.sessions} sessions (above {warn_above:.1f})")
    return corr


def sizing_summary(account: Optional[float], rows: list[SizingRow], corr: Optional[Correlation], cfg: dict[str, Any]) -> str:
    s = cfg
    lines = [f"[bold]Position sizing[/] (book p. 65: sizing never changes the signal; it only decides how much capital goes in)"]
    if not account or account <= 0:
        lines.append("Enter an account size above and press Enter.")
    else:
        lines.append(f"account ${account:,.0f}, risk {s['risk_pct']:g}% = ${account * s['risk_pct'] / 100:,.0f} per position; stop at {s['atr_multiple']:g} x ATR({s['atr_sessions']})")
        lines.append(f"[dim]{'ticker':<7}{'price':>9}{'ATR':>8}{'stop':>8}{'shares':>8}{'ask':>8}{'contracts':>10}[/]")
        for r in rows:
            lines.append(
                f"{r.ticker:<7}{_m(r.price):>9}{_m(r.atr):>8}{_m(r.stop_distance):>8}{_n(r.shares):>8}{_m(r.ask):>8}{_n(r.contracts):>10}"
                + (f"  [yellow]{r.note}[/]" if r.note else "")
            )
        lines.append("[dim]shares = account x risk / (multiple x ATR); contracts = account x risk / (ask x 100); both rounded down[/]")
    lines.append("")
    lines.append(f"[bold]Correlation[/] of daily returns over {corr.sessions if corr else 0} sessions (warning above {s['correlation_warn']:.1f})")
    if corr is None or not corr.matrix:
        lines.append((corr.note if corr else "no data"))
    else:
        head = "       " + "".join(f"{t:>7}" for t in corr.tickers)
        lines.append(f"[dim]{head}[/]")
        for a in corr.tickers:
            lines.append(f"{a:<7}" + "".join(f"{corr.matrix[(a, b)]:>7.2f}" for b in corr.tickers))
        for w in corr.warnings:
            lines.append(f"[yellow]{w}[/]")
        if corr.note:
            lines.append(f"[yellow]{corr.note}[/]")
    return "\n".join(lines)


def _m(x: Optional[float]) -> str:
    return "--" if x is None else f"{x:,.2f}"


def _n(x: Optional[int]) -> str:
    return "--" if x is None else f"{x:,d}"
