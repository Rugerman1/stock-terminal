"""The walk-forward backtest: the price factors replayed on past closes, fed to the same three tests.

Every `step` sessions, the universe is scored with the closes known on that date only
(momentum, trend and low risk; value and quality are left out because free
fundamentals are not point-in-time, and news is left out because Alpaca's history is
press releases only and would take hours to score, both said on screen). The top and
bottom 20 percent get labels, each label is scored `horizon` sessions later against
SPY exactly as a live signal is, and the whole set goes through the binomial, Monte
Carlo and BEF tests. The period returns (the average excess return of that date's
labels, in their direction) give the Sharpe ratio the experiment log records.

Survivorship bias is built in and stated: the replay uses today's universe list for
the whole period. Weights and thresholds are the live ones from config.yaml, never
fitted here.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import numpy as np

from .factors import FactorValue, low_risk, momentum, trend
from .scoring import FAVORABLE, UNFAVORABLE, RawFactors, score_all
from .signal_scorer import BENCHMARK, outcome_for
from .store import LONG_TERM, MarketStore
from .validity import ScoredSignal, Trade, ValidityReport, alternate, bef, build_report

EXCLUDED = {
    "value": "free fundamentals are not point-in-time (today's figures would leak into the past)",
    "quality": "free fundamentals are not point-in-time (today's figures would leak into the past)",
    "news": "Alpaca's news history is press releases only and would take hours to score; set backtest.replay_news to revisit",
}


@dataclass
class BacktestResult:
    start: Optional[str] = None
    end: Optional[str] = None
    rebalances: int = 0
    signals: int = 0
    wins: int = 0
    report: Optional[ValidityReport] = None
    period_returns: list[float] = field(default_factory=list)
    sharpe: Optional[float] = None            # annualised: per-period Sharpe x sqrt(periods a year)
    sharpe_per_period: Optional[float] = None
    skew: Optional[float] = None
    kurtosis: Optional[float] = None          # plain (3 for a normal distribution)
    excluded: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    params_hash: str = ""
    run_at: Optional[datetime] = None
    factors_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        r = self.report
        return {
            "start": self.start, "end": self.end, "rebalances": self.rebalances, "signals": self.signals, "wins": self.wins,
            "sharpe": self.sharpe, "sharpe_per_period": self.sharpe_per_period, "skew": self.skew, "kurtosis": self.kurtosis,
            "excluded": self.excluded, "notes": self.notes, "factors_used": self.factors_used, "period_returns": self.period_returns,
            "status": r.status if r else None,
            "binomial": {"n": r.binomial.n, "k": r.binomial.k, "p": r.binomial.probability} if r else None,
            "binomial_empirical": {"p": r.binomial_empirical.probability, "baseline": r.binomial_empirical.baseline} if r else None,
            "monte_carlo": {"p": r.monte_carlo.probability, "note": r.monte_carlo.note} if r else None,
            "bef_tickers": len(r.bef) if r else 0,
            "bef_significant": sum(1 for t in r.bef.values() if t.probability is not None and t.probability < 0.05) if r else 0,
        }


def params_for(cfg: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The settings that define a trial. Any change makes a new experiment-log entry."""
    q, b = cfg["quant"], cfg["backtest"]
    params = {
        "weights": {k: float(v) for k, v in q["weights"].items()},
        "zscore_cap": float(q["zscore_cap"]), "label_top_pct": float(q["label_top_pct"]), "label_bottom_pct": float(q["label_bottom_pct"]),
        "step_sessions": int(b["step_sessions"]), "horizon_sessions": int(b["horizon_sessions"]), "min_history_sessions": int(b["min_history_sessions"]),
        "replay_news": bool(b["replay_news"]), "factors": ["momentum", "trend", "low_risk"],
    }
    digest = hashlib.sha1(json.dumps(params, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return params, digest


def moments(values: list[float]) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """(mean, sample std, skew, kurtosis) of the period returns; None when there are fewer than three."""
    if len(values) < 3:
        return None, None, None, None
    x = np.asarray(values, dtype=float)
    m = float(x.mean())
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return m, sd, None, None
    d = x - m
    m2 = float((d ** 2).mean())
    skew = float((d ** 3).mean() / m2 ** 1.5)
    kurt = float((d ** 4).mean() / m2 ** 2)
    return m, sd, skew, kurt


def run_backtest(store: MarketStore, universe: list[str], cfg: dict[str, Any], as_of: date, progress=None, run_at: Optional[datetime] = None) -> BacktestResult:
    q, b = cfg["quant"], cfg["backtest"]
    params, digest = params_for(cfg)
    result = BacktestResult(params=params, params_hash=digest, run_at=run_at or datetime.now(timezone.utc), excluded=dict(EXCLUDED), factors_used=["momentum", "trend", "low_risk"])
    members = [u for u in universe if u != BENCHMARK]
    limit = int(b["history_days"])
    cutoff = as_of.isoformat()
    closes = {s: [(d, c) for d, c in store.closes(s, limit) if d <= cutoff] for s in members + [BENCHMARK]}
    spy = closes[BENCHMARK]
    if not spy:
        result.notes.append(f"no {BENCHMARK} closes stored")
        return result
    dates = [d for d, _ in spy]
    spy_at = dict(spy)
    at = {s: dict(rows) for s, rows in closes.items()}
    step, horizon, min_hist = int(b["step_sessions"]), int(b["horizon_sessions"]), int(b["min_history_sessions"])
    first, last = min_hist, len(dates) - 1 - horizon
    if last < first:
        result.notes.append(f"needs {min_hist + horizon + 1} sessions of {BENCHMARK} closes, have {len(dates)}")
        return result
    weights = dict(q["weights"])
    scored: list[ScoredSignal] = []
    period_returns: list[float] = []
    rebalance_dates = list(range(first, last + 1, step))
    for n_done, i in enumerate(rebalance_dates, 1):
        d, d_end = dates[i], dates[i + horizon]
        raws: dict[str, RawFactors] = {}
        for s in members:
            hist = [(dt, c) for dt, c in closes[s] if dt <= d]
            if not hist or hist[-1][0] != d:
                continue   # no close on the rebalance date: not scored that period
            raws[s] = RawFactors(
                symbol=s, as_of=d, price=hist[-1][1],
                values={"momentum": momentum(hist), "trend": trend(hist), "low_risk": low_risk(hist),
                        "value": FactorValue("value", None, "left out of the backtest"), "news": FactorValue("news", None, "left out of the backtest")},
                quality={},
            )
        scores = score_all(raws, members, weights, float(q["zscore_cap"]), float(q["label_top_pct"]), float(q["label_bottom_pct"]))
        spy_start, spy_end = spy_at.get(d), spy_at.get(d_end)
        pools: dict[int, list[int]] = {}
        for direction in (1, -1):
            pool = []
            for s in members:
                ps, pe = at[s].get(d), at[s].get(d_end)
                if ps and pe:
                    o, _, _, _ = outcome_for(LONG_TERM, direction, ps, pe, spy_start, spy_end, None)
                    if o is not None:
                        pool.append(o)
            pools[direction] = pool
        excess: list[float] = []
        for s, score in scores.items():
            direction = 1 if score.label == FAVORABLE else -1 if score.label == UNFAVORABLE else 0
            if direction == 0:
                continue
            ps, pe = at[s].get(d), at[s].get(d_end)
            if not ps or not pe or spy_start is None or spy_end is None:
                continue
            o, stock_ret, bench_ret, _ = outcome_for(LONG_TERM, direction, ps, pe, spy_start, spy_end, None)
            if o is None:
                continue
            scored.append(ScoredSignal(s, direction, o, _dt(d), _dt(d_end), pools[direction]))
            excess.append(direction * (stock_ret - bench_ret))
        if excess:
            period_returns.append(float(sum(excess) / len(excess)))
        if progress and (n_done % 4 == 0 or n_done == len(rebalance_dates)):
            progress(f"backtest {n_done}/{len(rebalance_dates)} rebalances")
    result.start, result.end = dates[first], dates[last + horizon]
    result.rebalances, result.signals, result.wins = len(rebalance_dates), len(scored), sum(s.outcome for s in scored)
    result.period_returns = period_returns
    mean, sd, skew, kurt = moments(period_returns)
    if mean is not None and sd:
        result.sharpe_per_period = mean / sd
        result.sharpe = mean / sd * math.sqrt(252.0 / step)
    result.skew, result.kurtosis = skew, kurt
    vcfg = dict(cfg["validity"])
    vcfg["window"] = max(len(scored), 1)   # the backtest uses every replayed signal, not just the newest 50

    def gap(ticker: str, direction: int, a: datetime, b_: datetime) -> Optional[int]:
        da, db = a.date().isoformat(), b_.date().isoformat()
        ps, pe = at.get(ticker, {}).get(da), at.get(ticker, {}).get(db)
        if not ps or not pe:
            return None
        o, _, _, _ = outcome_for(LONG_TERM, direction, ps, pe, spy_at.get(da), spy_at.get(db), None)
        return o

    result.report = build_report("backtest", scored, 0, vcfg, gap)
    result.notes.append("survivorship bias: today's universe list is used for the whole period")
    result.notes.append(f"labels every {step} sessions, scored {horizon} sessions later against {BENCHMARK}; factors {', '.join(result.factors_used)} only")
    return result


def _dt(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def backtest_summary(d: Optional[dict[str, Any]]) -> str:
    """From the stored dict (BacktestResult.to_dict plus run_at and params_hash), so a saved run reads the same as a fresh one."""
    if not d:
        return "No backtest yet."
    if not d.get("signals"):
        return "[bold]Backtest[/]: " + ("; ".join(d.get("notes") or []) or "nothing to replay")
    b, be, mc = d.get("binomial") or {}, d.get("binomial_empirical") or {}, d.get("monte_carlo") or {}
    lines = [
        f"[bold]Backtest[/] {d.get('start')} to {d.get('end')}: {d.get('rebalances')} rebalances, {d['signals']} replayed labels, {d.get('wins')} wins ({d.get('wins', 0) / d['signals'] * 100:.0f}%)",
        f"binomial at 50%: {_p(b.get('p'))}" + (f"; at the universe's rate {be['baseline'] * 100:.1f}%: {_p(be.get('p'))}" if be.get("baseline") is not None else ""),
        f"Monte Carlo: {_p(mc.get('p'))} of random runs did as well ({mc.get('note', '')})",
        f"BEF: {d.get('bef_significant', 0)} of {d.get('bef_tickers', 0)} names under 5%",
        (f"Sharpe {d['sharpe']:+.2f} annualised ({d['sharpe_per_period']:+.2f} per period, skew {d['skew']:+.2f}, kurtosis {d['kurtosis']:.2f})"
         if d.get("sharpe") is not None and d.get("skew") is not None else "Sharpe: fewer than three periods"),
        f"verdict on the replayed labels: [bold]{d.get('status')}[/]",
    ]
    for name, why in (d.get("excluded") or {}).items():
        lines.append(f"[yellow]{name} left out: {why}[/]")
    for n in d.get("notes") or []:
        lines.append(f"[dim]{n}[/]")
    if d.get("run_at"):
        lines.append(f"[dim]run {str(d['run_at'])[:16]}, trial {d.get('params_hash')}[/]")
    return "\n".join(lines)


def _p(x: Optional[float]) -> str:
    if x is None:
        return "--"
    return f"{x * 100:.5f}%" if x < 0.0001 else f"{x * 100:.2f}%"
