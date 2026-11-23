"""From raw factors to z-scores, the composite, the label, and the explain text.

z = (value - universe mean) / universe standard deviation, capped at +-3. Composite =
sum of weight x z over the six factors; a missing factor counts as z = 0 and is named
on screen. Top 20 percent of the universe by composite = Favorable, bottom 20 percent
= Unfavorable, the rest Neutral. Quality is the average of three z-scores (ROE, gross
margin, negative debt-to-equity) because its parts have different units; Value is the
plain average of two yields, which share a unit. Weights live in config.yaml and are
never tuned to past data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .factors import FactorValue, mean_std, zscore

FACTORS = ("momentum", "trend", "value", "quality", "low_risk", "news")
QUALITY_PARTS = ("roe", "gross_margin", "neg_debt_to_equity")
FAVORABLE, NEUTRAL, UNFAVORABLE = "Favorable", "Neutral", "Unfavorable"
SHORT_LABEL = {FAVORABLE: "FAV", NEUTRAL: "NEU", UNFAVORABLE: "UNF"}
FACTOR_TITLES = {
    "momentum": "momentum", "trend": "trend", "value": "value", "quality": "quality", "low_risk": "low risk", "news": "news",
    "roe": "ROE", "gross_margin": "gross margin", "neg_debt_to_equity": "-debt/equity",
}
FORMULAS = {
    "momentum": "price 21 trading days ago / price 252 trading days ago - 1",
    "trend": "price / 200-day simple average - 1",
    "value": "average of earnings yield (EPS TTM / price) and free-cash-flow yield (1 / price-to-FCF per share TTM)",
    "quality": "average of the z-scores of ROE, gross margin and negative debt-to-equity (mixed units, so each is z-scored first)",
    "low_risk": "negative of 1-year realized volatility (sample std of 252 daily log returns x sqrt 252)",
    "news": "time-weighted DIRECT headline sentiment, weight per headline = 0.5 ^ (age in days / 3), needs 5 or more headlines",
}


@dataclass
class RawFactors:
    """Everything known about one symbol before scoring: factor values and where they came from."""
    symbol: str
    as_of: Optional[str] = None                                    # date of the newest close used
    values: dict[str, FactorValue] = field(default_factory=dict)   # momentum, trend, low_risk, value, news
    quality: dict[str, FactorValue] = field(default_factory=dict)  # roe, gross_margin, neg_debt_to_equity
    sources: dict[str, str] = field(default_factory=dict)          # factor name -> source text for the explain view
    price: Optional[float] = None


@dataclass
class FactorScore:
    name: str
    weight: float
    raw: Optional[float]
    z: Optional[float]
    contribution: float
    mean: Optional[float] = None
    std: Optional[float] = None
    note: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    components: dict[str, "FactorScore"] = field(default_factory=dict)
    n: int = 0                                   # universe names behind the mean and std

    def to_dict(self) -> dict[str, Any]:
        out = {
            "weight": self.weight, "raw": self.raw, "z": self.z, "contribution": self.contribution,
            "mean": self.mean, "std": self.std, "n": self.n, "note": self.note, "inputs": self.inputs, "source": self.source,
        }
        if self.components:
            out["components"] = {k: v.to_dict() for k, v in self.components.items()}
        return out


@dataclass
class Score:
    symbol: str
    composite: Optional[float]
    label: Optional[str]
    percentile: Optional[float]
    factors: list[FactorScore]
    as_of: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    universe_n: int = 0
    scored_n: int = 0
    favorable_min: Optional[float] = None
    unfavorable_max: Optional[float] = None
    price: Optional[float] = None
    in_universe: bool = True

    @property
    def short_label(self) -> str:
        return SHORT_LABEL.get(self.label or "", "--")

    def factor(self, name: str) -> Optional[FactorScore]:
        return next((f for f in self.factors if f.name == name), None)

    def to_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "as_of": self.as_of, "composite": self.composite, "label": self.label,
            "percentile": self.percentile,
            "factors": {f.name: f.to_dict() for f in self.factors} | {"notes": self.notes, "price": self.price},
        }


def universe_stats(raws: dict[str, RawFactors], members: set[str]) -> dict[str, tuple[Optional[float], Optional[float], int]]:
    """Mean, sample std and count for every factor and quality part, over universe members that have the value."""
    stats: dict[str, tuple[Optional[float], Optional[float], int]] = {}
    for name in ("momentum", "trend", "low_risk", "value", "news"):
        values = [r.values[name].value for s, r in raws.items() if s in members and name in r.values and r.values[name].ok]
        avg, sd = mean_std(values)
        stats[name] = (avg, sd, len(values))
    for part in QUALITY_PARTS:
        values = [r.quality[part].value for s, r in raws.items() if s in members and part in r.quality and r.quality[part].ok]
        avg, sd = mean_std(values)
        stats[part] = (avg, sd, len(values))
    return stats


def _score_one(raw: RawFactors, stats: dict, weights: dict[str, float], cap: float) -> tuple[Optional[float], list[FactorScore], list[str]]:
    factors: list[FactorScore] = []
    notes: list[str] = []
    composite = 0.0
    any_factor = False
    for name in FACTORS:
        weight = float(weights.get(name, 0.0))
        if name == "quality":
            components: dict[str, FactorScore] = {}
            zs = []
            for part in QUALITY_PARTS:
                fv = raw.quality.get(part) or FactorValue(part, None, "not computed")
                avg, sd, n = stats.get(part, (None, None, 0))
                z = zscore(fv.value, avg, sd, cap)
                note = fv.note if not fv.ok else ("" if z is not None else f"no universe spread (n={n})")
                components[part] = FactorScore(part, 0.0, fv.value, z, 0.0, avg, sd, note, fv.inputs, n=n)
                if z is not None:
                    zs.append(z)
            z_quality = sum(zs) / len(zs) if zs else None
            raw_quality = None
            note = "" if len(zs) == 3 else (f"{len(zs)} of 3 parts available" if zs else "no quality parts available")
            contribution = weight * (z_quality or 0.0)
            factors.append(FactorScore("quality", weight, raw_quality, z_quality, contribution, None, None, note, {}, raw.sources.get("quality", ""), components))
        else:
            fv = raw.values.get(name) or FactorValue(name, None, "not computed")
            avg, sd, n = stats.get(name, (None, None, 0))
            z = zscore(fv.value, avg, sd, cap)
            note = fv.note if not fv.ok else ("" if z is not None else f"no universe spread (n={n})")
            contribution = weight * (z or 0.0)
            factors.append(FactorScore(name, weight, fv.value, z, contribution, avg, sd, note, fv.inputs, raw.sources.get(name, ""), n=n))
        last = factors[-1]
        if last.z is None:
            notes.append(f"{FACTOR_TITLES[name]} missing ({last.note}), counted as z = 0")
        else:
            any_factor = True
        composite += last.contribution
    return (composite if any_factor else None), factors, notes


def score_all(
    raws: dict[str, RawFactors],
    members: list[str],
    weights: dict[str, float],
    cap: float = 3.0,
    top_pct: float = 20.0,
    bottom_pct: float = 20.0,
) -> dict[str, Score]:
    """Score every symbol in `raws` against the universe `members`. Labels come from the members' composite spread."""
    member_set = {m.upper() for m in members}
    stats = universe_stats(raws, member_set)
    scored: dict[str, tuple[Optional[float], list[FactorScore], list[str]]] = {s: _score_one(r, stats, weights, cap) for s, r in raws.items()}
    member_comps = sorted(c for s, (c, _, _) in scored.items() if s in member_set and c is not None)
    n = len(member_comps)
    favorable_min = unfavorable_max = None
    if n >= 5:
        k_top = max(1, round(n * top_pct / 100.0))
        k_bottom = max(1, round(n * bottom_pct / 100.0))
        favorable_min = member_comps[-k_top]
        unfavorable_max = member_comps[k_bottom - 1]
    out: dict[str, Score] = {}
    for symbol, (composite, factors, notes) in scored.items():
        raw = raws[symbol]
        label = percentile = None
        if composite is not None and favorable_min is not None:
            below = sum(1 for c in member_comps if c < composite)
            percentile = 100.0 * below / n
            if composite >= favorable_min:
                label = FAVORABLE
            elif composite <= unfavorable_max:
                label = UNFAVORABLE
            else:
                label = NEUTRAL
        elif composite is not None:
            notes = notes + [f"universe too small to set labels ({n} scored, need 5)"]
        out[symbol] = Score(
            symbol=symbol, composite=composite, label=label, percentile=percentile, factors=factors, as_of=raw.as_of,
            notes=notes, universe_n=len(member_set), scored_n=n, favorable_min=favorable_min, unfavorable_max=unfavorable_max,
            price=raw.price, in_universe=symbol in member_set,
        )
    return out


# ----- text for the Score tab and the explain view -----

def _pct(x: Optional[float], digits: int = 1) -> str:
    return "--" if x is None else f"{x * 100:+.{digits}f}%"


def _num(x: Optional[float], digits: int = 2, sign: bool = True) -> str:
    if x is None:
        return "--"
    return f"{x:+.{digits}f}" if sign else f"{x:.{digits}f}"


def raw_text(name: str, raw: Optional[float]) -> str:
    """A factor's raw value in its natural unit."""
    if raw is None:
        return "--"
    if name in ("momentum", "trend", "value", "low_risk"):
        return _pct(raw)
    if name in ("roe", "gross_margin"):
        return f"{raw:.1f}%"
    if name == "news":
        return _num(raw)
    return _num(raw, 2)


def score_summary(score: Optional[Score], validity: str = "UNPROVEN 0/30") -> str:
    """The Score tab: label, composite, rank, one line per factor. Rich markup."""
    if score is None:
        return "No score yet."
    lines = []
    head = f"[bold]{score.symbol}[/]  "
    if score.label:
        head += f"[bold]{score.label}[/]  composite {_num(score.composite)}  {score.percentile:.0f}th pct of {score.scored_n}"
    elif score.composite is not None:
        head += f"composite {_num(score.composite)} (no label: {score.notes[-1] if score.notes else 'universe not scored'})"
    else:
        head += "no score (" + "; ".join(score.notes[:2]) + ")"
    lines.append(head)
    lines.append(f"as of {score.as_of or '?'} close  validity {validity}" + ("" if score.in_universe else "  (not in the universe)"))
    lines.append(f"[dim]{'factor':<10}{'w':>5}{'raw':>9}{'z':>7}{'w*z':>7}[/]")
    for f in score.factors:
        raw = "3 parts" if f.name == "quality" else raw_text(f.name, f.raw)
        lines.append(f"{FACTOR_TITLES[f.name]:<10}{f.weight:>5.2f}{raw:>9}{_num(f.z):>7}{_num(f.contribution):>7}")
    if score.favorable_min is not None:
        lines.append(f"[dim]Favorable >= {_num(score.favorable_min)}  Unfavorable <= {_num(score.unfavorable_max)}[/]")
    for note in score.notes:
        lines.append(f"[yellow]{note}[/]")
    lines.append("[dim]? shows the formula, every input and its source[/]")
    return "\n".join(lines)


def explain_text(score: Score, universe_name: str = "universe", validity: str = "UNPROVEN 0/30") -> str:
    """The ? view: every formula, every number that went in, where it came from. Rich markup."""
    lines = [
        f"[bold]Long-term score for {score.symbol}[/]  as of {score.as_of or '?'} close",
        "",
        "Composite = sum of weight x z over six factors.",
        "z = (stock's value - universe mean) / universe standard deviation, capped at -3 and +3.",
        f"Universe: {universe_name}, {score.scored_n} of {score.universe_n} names scored (sample standard deviation).",
        "A missing factor counts as z = 0 and is listed at the end.",
        "",
    ]
    if score.label:
        lines.append(
            f"Label: top 20% of the universe by composite = Favorable (composite >= {_num(score.favorable_min)}), "
            f"bottom 20% = Unfavorable (<= {_num(score.unfavorable_max)}), the rest Neutral."
        )
        lines.append(f"[bold]Composite {_num(score.composite)} -> {score.label}[/] ({score.percentile:.0f}th percentile of the universe)")
    else:
        lines.append(f"[bold]Composite {_num(score.composite)}[/], no label: " + ("; ".join(score.notes) or "universe not scored"))
    lines.append(f"Validity of this label: {validity}")
    lines.append("")
    for f in score.factors:
        lines.append(f"[bold]{FACTOR_TITLES[f.name]}[/]  weight {f.weight:.2f}")
        lines.append(f"  formula: {FORMULAS[f.name]}")
        if f.name == "quality":
            for part, c in f.components.items():
                if c.raw is None:
                    lines.append(f"  {FACTOR_TITLES[part]}: missing ({c.note})")
                else:
                    lines.append(
                        f"  {FACTOR_TITLES[part]} = {raw_text(part, c.raw)}   universe mean {raw_text(part, c.mean)}, std {raw_text(part, c.std)} (n={c.n})   z {_num(c.z)}"
                    )
            lines.append(f"  quality z = average of available part z-scores = {_num(f.z)}" + (f"   ({f.note})" if f.note else ""))
        else:
            if f.raw is None:
                lines.append(f"  value: missing ({f.note})")
            else:
                lines.append(f"  inputs: {_inputs_text(f.name, f.inputs)}")
                lines.append(
                    f"  value {raw_text(f.name, f.raw)}   universe mean {raw_text(f.name, f.mean)}, std {raw_text(f.name, f.std)} (n={f.n})   z {_num(f.z)}"
                    + (f"   ({f.note})" if f.note else "")
                )
        lines.append(f"  contribution = {f.weight:.2f} x {_num(f.z) if f.z is not None else '0 (missing)'} = {_num(f.contribution)}")
        if f.source:
            lines.append(f"  [dim]source: {f.source}[/]")
        lines.append("")
    if score.notes:
        lines.append("[yellow]Missing or limited:[/]")
        lines.extend(f"  {n}" for n in score.notes)
    return "\n".join(lines)


def _inputs_text(name: str, inputs: dict[str, Any]) -> str:
    if not inputs:
        return "--"
    if name == "momentum":
        return (f"price 21 days ago {inputs.get('price_21d_ago'):.2f} ({inputs.get('price_21d_ago_date')}), "
                f"price 252 days ago {inputs.get('price_252d_ago'):.2f} ({inputs.get('price_252d_ago_date')})")
    if name == "trend":
        return f"price {inputs.get('price'):.2f} ({inputs.get('price_date')}), 200-day average {inputs.get('avg_200d'):.2f}"
    if name == "low_risk":
        return f"1-year realized volatility {inputs.get('realized_vol_1y') * 100:.1f}% from {inputs.get('returns_used')} daily log returns"
    if name == "value":
        parts = []
        if "earnings_yield" in inputs:
            parts.append(f"EPS TTM {inputs['epsTTM']:.2f} / price {inputs['price']:.2f} = {_pct(inputs['earnings_yield'])}")
        if "fcf_yield" in inputs:
            parts.append(f"1 / P-to-FCF {inputs['pfcfShareTTM']:.2f} = {_pct(inputs['fcf_yield'])}")
        return "; ".join(parts) or "--"
    if name == "news":
        return (f"{inputs.get('headlines')} scored DIRECT headlines, half-life {inputs.get('halflife_days')} days, "
                f"newest {inputs.get('newest_age_days', 0):.1f} days old, sum of weights {inputs.get('sum_of_weights', 0):.2f}")
    return ", ".join(f"{k} {v}" for k, v in inputs.items())


def is_finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
