"""The six factors of the long-term score, as pure functions with their inputs kept for the explain view.

Every function returns a FactorValue: the number, or None with a note saying what was
missing. Nothing is estimated or filled in. Definitions follow the spec word for word;
where the spec leaves a choice (which standard deviation, which price), the choice is
written here and shown on screen.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean, stdev
from typing import Any, Optional

MOMENTUM_NEAR = 21     # trading days
MOMENTUM_FAR = 252
TREND_WINDOW = 200
VOL_WINDOW = 252
TRADING_DAYS = 252


@dataclass
class FactorValue:
    name: str
    value: Optional[float]
    note: str = ""                                   # why it is missing, or a remark
    inputs: dict[str, Any] = field(default_factory=dict)   # what went into it, for the explain view

    @property
    def ok(self) -> bool:
        return self.value is not None


def momentum(closes: list[tuple[str, float]]) -> FactorValue:
    """price 21 trading days ago / price 252 trading days ago - 1. Needs 253 closes."""
    need = MOMENTUM_FAR + 1
    if len(closes) < need:
        return FactorValue("momentum", None, f"needs {need} daily closes, have {len(closes)}")
    near_date, near = closes[-1 - MOMENTUM_NEAR]
    far_date, far = closes[-1 - MOMENTUM_FAR]
    if far <= 0:
        return FactorValue("momentum", None, "price 252 days ago is not positive")
    return FactorValue(
        "momentum", near / far - 1.0,
        inputs={"price_21d_ago": near, "price_21d_ago_date": near_date, "price_252d_ago": far, "price_252d_ago_date": far_date},
    )


def trend(closes: list[tuple[str, float]]) -> FactorValue:
    """price / 200-day average - 1. The average is the simple mean of the last 200 closes, today's included."""
    if len(closes) < TREND_WINDOW:
        return FactorValue("trend", None, f"needs {TREND_WINDOW} daily closes, have {len(closes)}")
    last_date, last = closes[-1]
    avg = mean(c for _, c in closes[-TREND_WINDOW:])
    if avg <= 0:
        return FactorValue("trend", None, "200-day average is not positive")
    return FactorValue("trend", last / avg - 1.0, inputs={"price": last, "price_date": last_date, "avg_200d": avg})


def realized_volatility(closes: list[tuple[str, float]], window: int = VOL_WINDOW) -> Optional[float]:
    """Annualised standard deviation of daily log returns over the last `window` returns (sample standard deviation)."""
    if len(closes) < window + 1:
        return None
    prices = [c for _, c in closes[-(window + 1):]]
    if any(p <= 0 for p in prices):
        return None
    returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    return stdev(returns) * math.sqrt(TRADING_DAYS)


def low_risk(closes: list[tuple[str, float]]) -> FactorValue:
    """Negative of 1-year realized volatility (252 daily log returns, sample standard deviation, times sqrt(252))."""
    vol = realized_volatility(closes)
    if vol is None:
        return FactorValue("low_risk", None, f"needs {VOL_WINDOW + 1} daily closes, have {len(closes)}")
    return FactorValue("low_risk", -vol, inputs={"realized_vol_1y": vol, "returns_used": VOL_WINDOW})


def _num(metrics: dict[str, Any], key: str) -> Optional[float]:
    raw = metrics.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def value(metrics: Optional[dict[str, Any]], price: Optional[float]) -> FactorValue:
    """Average of earnings yield (EPS TTM / price) and free-cash-flow yield (1 / price-to-FCF per share TTM).

    Both are decimals (0.03 = 3 percent). A negative EPS or FCF gives a negative yield, which is the honest reading.
    With only one of the two available, that one is used and the note says so.
    """
    if not metrics:
        return FactorValue("value", None, "no fundamentals")
    inputs: dict[str, Any] = {}
    parts = []
    eps = _num(metrics, "epsTTM")
    if eps is not None and price:
        ey = eps / price
        parts.append(ey)
        inputs.update({"epsTTM": eps, "price": price, "earnings_yield": ey})
    pfcf = _num(metrics, "pfcfShareTTM")
    if pfcf is not None and pfcf != 0:
        fy = 1.0 / pfcf
        parts.append(fy)
        inputs.update({"pfcfShareTTM": pfcf, "fcf_yield": fy})
    if not parts:
        return FactorValue("value", None, "no epsTTM or pfcfShareTTM in the fundamentals", inputs)
    note = "" if len(parts) == 2 else "only one of the two yields available"
    return FactorValue("value", mean(parts), note, inputs)


def quality_components(metrics: Optional[dict[str, Any]]) -> dict[str, FactorValue]:
    """ROE (percent), gross margin (percent) and negative debt-to-equity (ratio). Each is z-scored on its own, then averaged."""
    out = {
        "roe": FactorValue("roe", None, "no roeTTM"),
        "gross_margin": FactorValue("gross_margin", None, "no grossMarginTTM"),
        "neg_debt_to_equity": FactorValue("neg_debt_to_equity", None, "no totalDebt/totalEquityAnnual"),
    }
    if not metrics:
        return out
    roe = _num(metrics, "roeTTM")
    if roe is not None:
        out["roe"] = FactorValue("roe", roe, inputs={"roeTTM": roe})
    gm = _num(metrics, "grossMarginTTM")
    if gm is not None:
        out["gross_margin"] = FactorValue("gross_margin", gm, inputs={"grossMarginTTM": gm})
    de = _num(metrics, "totalDebt/totalEquityAnnual")
    if de is None:
        de = _num(metrics, "totalDebt/totalEquityQuarterly")
    if de is not None:
        out["neg_debt_to_equity"] = FactorValue("neg_debt_to_equity", -de, inputs={"debt_to_equity": de})
    return out


def news_sentiment(headlines: list[tuple[float, float]], halflife_days: float = 3.0, min_headlines: int = 5) -> FactorValue:
    """Time-weighted DIRECT sentiment. headlines: (age in days, sentiment). Weight = 0.5 ^ (age / halflife)."""
    usable = [(age, s) for age, s in headlines if s is not None and age is not None and age >= 0]
    if len(usable) < min_headlines:
        return FactorValue("news", None, f"needs {min_headlines} scored DIRECT headlines, have {len(usable)}", {"headlines": len(usable)})
    weights = [0.5 ** (age / halflife_days) for age, _ in usable]
    total = sum(weights)
    weighted = sum(w * s for w, (_, s) in zip(weights, usable)) / total
    return FactorValue(
        "news", weighted,
        inputs={"headlines": len(usable), "halflife_days": halflife_days, "sum_of_weights": total, "newest_age_days": min(a for a, _ in usable)},
    )


def mean_std(values: list[float]) -> tuple[Optional[float], Optional[float]]:
    """Universe mean and sample standard deviation. None when fewer than two values."""
    if len(values) < 2:
        return (values[0] if values else None), None
    return mean(values), stdev(values)


def zscore(x: Optional[float], avg: Optional[float], sd: Optional[float], cap: float = 3.0) -> Optional[float]:
    """(x - mean) / std, capped at -cap and +cap. None when any input is missing or the spread is zero."""
    if x is None or avg is None or sd is None or sd <= 0:
        return None
    return max(-cap, min(cap, (x - avg) / sd))
