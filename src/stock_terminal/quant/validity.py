"""The validity engine, from Berdondini's method (pages 36 to 42, 51 to 58).

A result counts only when the chance of matching it by luck is small. Every label the
app produced is scored 1 or 0 once its horizon passes (a long-term label wins when the
stock beat SPY in the predicted direction; an options candidate wins when the stock
ended beyond the breakeven on the predicted side), and the last 50 scored signals go
through three tests:

1. Binomial: the probability of k or more wins in n tries by luck, P(X >= k), at the
   50 percent baseline the spec sets for beat-SPY outcomes, and beside it at the
   empirical rate (how often a random universe name won over the same windows).
2. Monte Carlo: 10,000 runs of random signals with the same count, windows and
   directions, each a random universe name; the share of runs that did as well or better.
3. Binomial Evolution Function, per ticker: force buy/sell alternation by inserting an
   opposite test trade in each gap, convert to 1/0, keep only trades that sit in a
   consecutive same-result pair, binomial test at 50 percent.

Status: UNPROVEN under 30 scored, RANDOM-LIKE when the probability is above 5 percent
(the book's virtual mode: keep recording, stop trusting), WEAK EVIDENCE between 1 and 5
percent, SIGNIFICANT below 1 percent. The headline probability is the pooled binomial
at the 50 percent baseline; the others are shown next to it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import numpy as np

UNPROVEN, RANDOM_LIKE, WEAK, SIGNIFICANT = "UNPROVEN", "RANDOM-LIKE", "WEAK EVIDENCE", "SIGNIFICANT"


# ----- binomial sums, exact -----

def binomial_pmf(k: int, n: int, p: float) -> float:
    if k < 0 or k > n:
        return 0.0
    return math.comb(n, k) * (p ** k) * ((1.0 - p) ** (n - k))


def prob_at_least(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    return float(sum(binomial_pmf(i, n, p) for i in range(k, n + 1)))


def prob_at_most(k: int, n: int, p: float) -> float:
    """P(X <= k)."""
    if k >= n:
        return 1.0
    return float(sum(binomial_pmf(i, n, p) for i in range(0, k + 1)))


def prob_more_than(k: int, n: int, p: float) -> float:
    """P(X > k)."""
    return prob_at_least(k + 1, n, p)


def prob_behind(n: int, p: float) -> float:
    """The book's example (p. 40): the chance of having strictly fewer wins than losses after n tries."""
    return prob_at_most((n - 1) // 2 if n % 2 else n // 2 - 1, n, p)


# ----- Binomial Evolution Function -----

@dataclass
class Trade:
    direction: int          # +1 long side, -1 short side
    outcome: int            # 1 win, 0 loss
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    virtual: bool = False   # an inserted test trade


def alternate(trades: list[Trade], gap_outcome) -> list[Trade]:
    """Step 1: between two consecutive trades on the same side, insert the opposite test trade over the gap.

    `gap_outcome(direction, start, end)` scores the test trade from the market move over the gap (1 or 0), or
    returns None when it cannot be scored, in which case the gap is left alone.
    """
    out: list[Trade] = []
    for i, trade in enumerate(trades):
        if out and out[-1].direction == trade.direction and out[-1].end is not None and trade.start is not None and trade.start >= out[-1].end:
            result = gap_outcome(-trade.direction, out[-1].end, trade.start)
            if result is not None:
                out.append(Trade(-trade.direction, int(result), out[-1].end, trade.start, virtual=True))
        out.append(trade)
    return out


def paired_outcomes(outcomes: list[int]) -> list[int]:
    """Step 3: keep only the trades that sit in a consecutive pair with the same result."""
    keep = [False] * len(outcomes)
    for i in range(len(outcomes) - 1):
        if outcomes[i] == outcomes[i + 1]:
            keep[i] = keep[i + 1] = True
    return [o for o, k in zip(outcomes, keep) if k]


def bef(outcomes: list[int]) -> tuple[int, int, Optional[float]]:
    """Steps 3 and 4 on an already-alternating 1/0 sequence: (n kept, k wins, P(X >= k) at 50%). Spec example:
    [1, 0, 1, 0, 1, 1, 0] -> 2 kept, 2 wins, 25 percent."""
    kept = paired_outcomes([int(o) for o in outcomes])
    n, k = len(kept), sum(kept)
    return n, k, (prob_at_least(k, n, 0.5) if n else None)


# ----- status -----

def status_for(n_scored: int, probability: Optional[float], cfg: dict[str, Any]) -> str:
    if n_scored < int(cfg["min_signals"]) or probability is None:
        return UNPROVEN
    if probability > float(cfg["random_like_above"]):
        return RANDOM_LIKE
    if probability < float(cfg["significant_below"]):
        return SIGNIFICANT
    return WEAK


# ----- the report -----

@dataclass
class ScoredSignal:
    """A scored signal with what the tests need: its window, direction, outcome, and the universe's outcomes over the window."""
    ticker: str
    direction: int
    outcome: int
    start: datetime
    end: datetime
    universe_outcomes: list[int] = field(default_factory=list)   # one per universe name that could be scored on the same window


@dataclass
class TestResult:
    n: int = 0
    k: int = 0
    probability: Optional[float] = None
    baseline: Optional[float] = None
    note: str = ""


@dataclass
class ValidityReport:
    kind: str
    status: str
    n_scored: int
    n_open: int
    min_signals: int
    window: int
    binomial: TestResult = field(default_factory=TestResult)            # at the 50% baseline
    binomial_empirical: TestResult = field(default_factory=TestResult)  # at the universe's own win rate over the same windows
    monte_carlo: TestResult = field(default_factory=TestResult)
    bef: dict[str, TestResult] = field(default_factory=dict)            # per ticker
    per_ticker: dict[str, TestResult] = field(default_factory=dict)     # binomial per ticker at 50%
    notes: list[str] = field(default_factory=list)

    @property
    def column_text(self) -> str:
        if self.status == UNPROVEN:
            return f"UNPROVEN {self.n_scored}/{self.min_signals}"
        return self.status

    @property
    def virtual_mode(self) -> bool:
        return self.status == RANDOM_LIKE


def monte_carlo(signals: list[ScoredSignal], k_real: int, runs: int, seed: int) -> TestResult:
    """Random signals with the same count, windows and directions: a random universe name per signal, scored the same way."""
    usable = [s for s in signals if s.universe_outcomes]
    if len(usable) < len(signals) or not usable:
        return TestResult(len(signals), k_real, None, None, f"{len(signals) - len(usable)} signals have no universe outcomes for their window")
    rng = np.random.default_rng(seed)
    wins = np.zeros(runs, dtype=np.int64)
    for s in usable:
        pool = np.asarray(s.universe_outcomes, dtype=np.int64)
        wins += pool[rng.integers(0, len(pool), size=runs)]
    share = float(np.mean(wins >= k_real))
    return TestResult(len(usable), k_real, share, None, f"{runs:,} runs")


def build_report(kind: str, scored: list[ScoredSignal], n_open: int, cfg: dict[str, Any], gap_outcome=None) -> ValidityReport:
    """The three tests on the newest `window` scored signals (oldest first inside the window)."""
    window = int(cfg["window"])
    recent = sorted(scored, key=lambda s: s.end)[-window:]
    n, k = len(recent), sum(s.outcome for s in recent)
    report = ValidityReport(kind=kind, status=UNPROVEN, n_scored=len(scored), n_open=n_open, min_signals=int(cfg["min_signals"]), window=window)
    if n == 0:
        report.notes.append("no scored signals yet")
        return report
    report.binomial = TestResult(n, k, prob_at_least(k, n, 0.5), 0.5, "P(X >= k) at the 50% baseline")
    rates = [sum(s.universe_outcomes) / len(s.universe_outcomes) for s in recent if s.universe_outcomes]
    if rates:
        empirical = float(sum(rates) / len(rates))
        report.binomial_empirical = TestResult(n, k, prob_at_least(k, n, empirical), empirical, f"P(X >= k) at the universe's own rate over the same windows ({len(rates)} of {n} windows)")
    else:
        report.binomial_empirical = TestResult(n, k, None, None, "no universe outcomes for these windows")
    report.monte_carlo = monte_carlo(recent, k, int(cfg["monte_carlo_runs"]), int(cfg["seed"]))
    by_ticker: dict[str, list[ScoredSignal]] = {}
    for s in recent:
        by_ticker.setdefault(s.ticker, []).append(s)
    for ticker, items in sorted(by_ticker.items()):
        items.sort(key=lambda s: s.start)
        tk = sum(s.outcome for s in items)
        report.per_ticker[ticker] = TestResult(len(items), tk, prob_at_least(tk, len(items), 0.5), 0.5)
        trades = [Trade(s.direction, s.outcome, s.start, s.end) for s in items]
        if gap_outcome is not None:
            trades = alternate(trades, lambda d, a, b, t=ticker: gap_outcome(t, d, a, b))
        bn, bk, bp = bef([t.outcome for t in trades])
        inserted = sum(1 for t in trades if t.virtual)
        report.bef[ticker] = TestResult(bn, bk, bp, 0.5, f"{len(items)} real trades, {inserted} test trades inserted, {bn} kept in same-result pairs")
    report.status = status_for(len(scored), report.binomial.probability, cfg)
    return report


# ----- text -----

def _p(x: Optional[float]) -> str:
    if x is None:
        return "--"
    if x < 0.0001:
        return f"{x * 100:.5f}%"
    return f"{x * 100:.2f}%"


def validity_summary(report: Optional[ValidityReport]) -> str:
    if report is None:
        return "No validity report yet."
    r = report
    colour = {UNPROVEN: "dim", RANDOM_LIKE: "bold red", WEAK: "yellow", SIGNIFICANT: "bold green"}[r.status]
    lines = [f"[bold]{r.kind}[/]  [{colour}]{r.status}[/]  {r.n_scored} scored, {r.n_open} open, needs {r.min_signals}; tests use the last {r.window}"]
    if r.binomial.n:
        lines.append(f"binomial: {r.binomial.k} wins of {r.binomial.n}, P(X >= k) at 50% = {_p(r.binomial.probability)}")
        if r.binomial_empirical.baseline is not None:
            lines.append(f"  at the universe's rate {r.binomial_empirical.baseline * 100:.1f}%: {_p(r.binomial_empirical.probability)}")
        lines.append(f"Monte Carlo: share of random runs as good or better = {_p(r.monte_carlo.probability)} ({r.monte_carlo.note})")
        for ticker, t in r.per_ticker.items():
            b = r.bef.get(ticker)
            bef_text = f"BEF {b.k}/{b.n} -> {_p(b.probability)}" if b and b.n else "BEF: no same-result pairs yet"
            lines.append(f"  {ticker}: {t.k}/{t.n} wins, P {_p(t.probability)}; {bef_text}")
    for note in r.notes:
        lines.append(f"[yellow]{note}[/]")
    if r.virtual_mode:
        lines.append("[bold red]Virtual mode: the labels could be luck. Keep recording, stop trusting.[/]")
    return "\n".join(lines)


def validity_explain() -> str:
    return "\n".join([
        "[bold]How the honesty check works[/] (Berdondini, The Theory of Quantitative Trading, pp. 36-42 and 51-58)",
        "A label is logged when it appears and scored 1 or 0 when its horizon passes, or earlier when the label changes (the system closing the bet).",
        "Long-term: 1 when the stock beat SPY in the predicted direction over the window. Options: 1 when the stock ended beyond the breakeven on the predicted side.",
        "Binomial: P(X >= k) = sum over i from k to n of C(n, i) p^i (1-p)^(n-i), the chance of k or more wins in n tries by luck.",
        "  p = 50% for beat-SPY outcomes; the second line uses the share of universe names that won over the same windows.",
        "Monte Carlo: 10,000 runs; each run replaces every real signal with a random universe name over the same window and direction; the share of runs with at least as many wins.",
        "BEF, per ticker: insert an opposite test trade in each gap between same-side trades, keep only trades in consecutive same-result pairs, binomial at 50%.",
        "Status: UNPROVEN under 30 scored; RANDOM-LIKE when the pooled binomial probability is above 5% (virtual mode); WEAK EVIDENCE between 1% and 5%; SIGNIFICANT below 1%.",
    ])
