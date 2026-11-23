"""The experiment log and the Deflated Sharpe Ratio (Bailey and Lopez de Prado, 2014).

Every distinct set of weights and thresholds that has been backtested is one trial.
The more trials, the higher the best Sharpe you would expect from luck alone, so the
Deflated Sharpe Ratio asks: given the number of trials and the spread of their Sharpe
ratios, what is the probability that the current strategy's Sharpe is really above
the best-by-luck level? Design call 3 in the project note: the log stores each
trial's Sharpe, skew and kurtosis because the formula needs them, not just a count.

Formulas (per-period Sharpe ratios, T observations):
  expected best Sharpe by luck  SR0 = sqrt(V[SR]) x ((1 - g) Z(1 - 1/N) + g Z(1 - 1/(N e))), g = 0.5772 (Euler-Mascheroni)
  deflated Sharpe               DSR = Phi( (SR - SR0) sqrt(T - 1) / sqrt(1 - skew SR + (kurtosis - 1) / 4 SR^2) )
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist, variance
from typing import Any, Optional

EULER = 0.5772156649015329
_NORMAL = NormalDist()


@dataclass
class DeflatedSharpe:
    n_trials: int
    sr: Optional[float]            # the current trial's per-period Sharpe
    sr0: Optional[float]           # the best Sharpe expected from luck over the trials
    dsr: Optional[float]           # the probability the true Sharpe beats SR0
    t: int = 0
    note: str = ""


def expected_max_sharpe(sharpes: list[float]) -> Optional[float]:
    """SR0: what the best of N random trials would score. None under two trials or with no spread."""
    values = [s for s in sharpes if s is not None]
    n = len(values)
    if n < 2:
        return None
    var = variance(values)
    if var <= 0:
        return 0.0
    return math.sqrt(var) * ((1.0 - EULER) * _NORMAL.inv_cdf(1.0 - 1.0 / n) + EULER * _NORMAL.inv_cdf(1.0 - 1.0 / (n * math.e)))


def probabilistic_sharpe(sr: float, sr0: float, t: int, skew: Optional[float], kurtosis: Optional[float]) -> Optional[float]:
    """PSR(SR0): the probability the true Sharpe exceeds SR0 given T observations, skew and kurtosis."""
    if t < 2:
        return None
    skew = 0.0 if skew is None else skew
    kurtosis = 3.0 if kurtosis is None else kurtosis
    denominator = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    if denominator <= 0:
        return None
    return _NORMAL.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denominator))


def deflated_sharpe(trials: list[dict[str, Any]], current_hash: str) -> DeflatedSharpe:
    """trials: the experiment-log rows (sharpe = per-period Sharpe, n_returns, skew, kurtosis). The current trial is found by hash."""
    current = next((t for t in trials if t.get("params_hash") == current_hash), None)
    n = len(trials)
    if current is None or current.get("sharpe") is None:
        return DeflatedSharpe(n, None, None, None, 0, "the current parameter set has no backtest Sharpe yet")
    sr, t = float(current["sharpe"]), int(current.get("n_returns") or 0)
    sr0 = expected_max_sharpe([float(x["sharpe"]) for x in trials if x.get("sharpe") is not None])
    if sr0 is None:
        return DeflatedSharpe(n, sr, None, None, t, "needs two or more trials with a Sharpe to estimate the best-by-luck level")
    dsr = probabilistic_sharpe(sr, sr0, t, current.get("skew"), current.get("kurtosis"))
    note = "" if dsr is not None else "too few periods, or a variance term that is not positive"
    return DeflatedSharpe(n, sr, sr0, dsr, t, note)


def experiments_summary(trials: list[dict[str, Any]], result: DeflatedSharpe) -> str:
    lines = [f"[bold]Experiment log[/]: {result.n_trials} trial{'s' if result.n_trials != 1 else ''} (one per distinct set of weights and thresholds backtested)"]
    if result.sr is not None:
        lines.append(f"current trial Sharpe {result.sr:+.3f} per period over {result.t} periods")
    if result.sr0 is not None:
        lines.append(f"best Sharpe expected by luck over {result.n_trials} trials: {result.sr0:+.3f}")
    if result.dsr is not None:
        lines.append(f"[bold]Deflated Sharpe Ratio: {result.dsr * 100:.1f}%[/] probability the true Sharpe beats that level")
    if result.note:
        lines.append(f"[yellow]{result.note}[/]")
    for t in trials[-5:]:
        sharpe = f"{t['sharpe']:+.3f}" if t.get("sharpe") is not None else "--"
        lines.append(f"[dim]{str(t.get('at', ''))[:16]}  {t.get('params_hash')}  Sharpe {sharpe} per period[/]")
    lines.append("[dim]Changing a weight or threshold in config.yaml adds a trial; the weights are the spec's and are not tuned to the backtest.[/]")
    return "\n".join(lines)
