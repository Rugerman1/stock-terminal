"""The call / put check, in the spec's four steps.

1. Direction: the average of three z-scores against the universe (63-day return, price
   versus the 50-day average, 7-day DIRECT news sentiment). Beyond +1 looks at calls,
   beyond -1 at puts, in between is No trade before any contract is touched.
2. Contract: 30 to 60 days to expiry, delta nearest 0.50, open interest 500 or more,
   bid-ask spread no wider than 10 percent of the mid.
3. Numbers: HV20 and HV60, the feed's implied volatility (or Black-Scholes solved from
   the mid), vol ratio, IV rank (collected one ATM IV a day), expected move, breakeven
   and the move it needs, two probabilities of profit, the simulated expected value, the
   max loss, and the Greeks (the feed's, else computed).
4. Label: Call candidate or Put candidate only when direction, vol ratio (1.2 or
   less), needed move within the expected move, and liquidity all pass. Otherwise No
   trade with the failed check named. Earnings before expiry gets a warning.
Every number keeps its source. Nothing here places, previews or sizes an order.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from ..datapoint import DataPoint
from . import blackscholes as bs
from .factors import realized_volatility

CALL, PUT = "call", "put"
CALL_CANDIDATE, PUT_CANDIDATE, NO_TRADE = "Call candidate", "Put candidate", "No trade"
SHORT_OPT = {CALL_CANDIDATE: "CALL", PUT_CANDIDATE: "PUT", NO_TRADE: "none"}


@dataclass
class DirectionScore:
    """Average of the three z-scores. Components keep raw value, z and the universe stats behind them."""
    value: Optional[float]
    components: dict[str, dict[str, Any]]   # name -> {raw, z, mean, std, n, note}
    threshold: float
    note: str = ""

    @property
    def side(self) -> Optional[str]:
        if self.value is None:
            return None
        if self.value >= self.threshold:
            return CALL
        if self.value <= -self.threshold:
            return PUT
        return None


@dataclass
class Contract:
    symbol: str
    kind: str
    strike: float
    expiry: date
    open_interest: int
    bid: Optional[float] = None
    ask: Optional[float] = None
    iv: Optional[float] = None          # the feed's implied volatility
    delta: Optional[float] = None       # the feed's delta
    greeks: dict[str, float] = field(default_factory=dict)
    quote_at: Optional[datetime] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        return 0.5 * (self.bid + self.ask)

    @property
    def spread_pct(self) -> Optional[float]:
        mid = self.mid
        if mid is None or mid <= 0:
            return None
        return (self.ask - self.bid) / mid * 100.0


@dataclass
class OptionsResult:
    ticker: str
    label: str
    side: Optional[str]
    direction: DirectionScore
    contract: Optional[Contract] = None
    metrics: dict[str, Any] = field(default_factory=dict)     # every computed number with its unit
    checks: dict[str, tuple[bool, str]] = field(default_factory=dict)   # name -> (passed, detail)
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)            # data problems
    sources: dict[str, str] = field(default_factory=dict)
    as_of: Optional[datetime] = None

    @property
    def short_label(self) -> str:
        return SHORT_OPT.get(self.label, "--")


def contracts_from_payload(payload: Optional[list[dict[str, Any]]], chain: Optional[dict[str, dict[str, Any]]]) -> list[Contract]:
    """Join the contract list (open interest, strike, expiry) with the chain snapshots (quote, IV, Greeks)."""
    out: list[Contract] = []
    chain = chain or {}
    for raw in payload or []:
        try:
            expiry = date.fromisoformat(str(raw["expiration_date"]))
            strike = float(raw["strike_price"])
        except (KeyError, TypeError, ValueError):
            continue
        oi_raw = raw.get("open_interest")
        try:
            oi = int(float(oi_raw)) if oi_raw not in (None, "") else 0
        except (TypeError, ValueError):
            oi = 0
        snap = chain.get(raw["symbol"]) or {}
        greeks = {k: float(v) for k, v in (snap.get("greeks") or {}).items() if v is not None}
        out.append(
            Contract(
                symbol=str(raw["symbol"]), kind=str(raw.get("type", "")).lower(), strike=strike, expiry=expiry, open_interest=oi,
                bid=snap.get("bid"), ask=snap.get("ask"), iv=snap.get("iv"), delta=greeks.get("delta"), greeks=greeks,
                quote_at=datetime.fromisoformat(snap["quote_at"]) if snap.get("quote_at") else None,
            )
        )
    return out


def pick_contract(contracts: list[Contract], kind: str, target_delta: float, min_oi: int, max_spread_pct: float) -> tuple[Optional[Contract], Optional[Contract]]:
    """(best liquid contract, best contract ignoring liquidity), both by delta nearest the target."""
    side = [c for c in contracts if c.kind == kind and c.delta is not None]
    if not side:
        return None, None
    by_delta = sorted(side, key=lambda c: abs(abs(c.delta) - target_delta))
    liquid = [c for c in by_delta if c.open_interest >= min_oi and c.spread_pct is not None and c.spread_pct <= max_spread_pct]
    return (liquid[0] if liquid else None), by_delta[0]


class OptionsCheck:
    """Runs steps 2 to 4 for one ticker given the direction score and the data handed in."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.o = cfg["options"]

    def run(
        self,
        ticker: str,
        price: Optional[float],
        closes: list[tuple[str, float]],
        direction: DirectionScore,
        contracts: list[Contract],
        contracts_status: DataPoint,
        rate: DataPoint,
        earnings: DataPoint,
        iv_rank: tuple[Optional[float], int],
        now: datetime,
    ) -> OptionsResult:
        o = self.o
        result = OptionsResult(ticker=ticker, label=NO_TRADE, side=direction.side, direction=direction, as_of=now)
        result.sources["direction"] = "z-scores against the universe from stored closes and DIRECT headline sentiment"
        if direction.value is None:
            result.failed.append(f"direction: {direction.note}")
        elif direction.side is None:
            result.failed.append(f"direction z {direction.value:+.2f} is inside +-{o['direction_threshold']:.1f}")
        kind = direction.side or CALL   # with no direction the ATM call is still analysed so IV, HV and IV rank show

        if price is None or price <= 0:
            result.notes.append("no price: no daily closes stored")
            result.failed.append("price missing")
            return result
        hv20 = realized_volatility(closes, 20)
        hv60 = realized_volatility(closes, 60)
        result.metrics["hv20"] = hv20
        result.metrics["hv60"] = hv60
        result.sources["hv"] = f"stored daily closes, {len(closes)} days"
        if hv60 is None:
            result.notes.append(f"HV60 needs 61 daily closes, have {len(closes)}")

        if not contracts_status.ok:
            result.notes.append(f"option chain: {contracts_status.note}")
            result.failed.append("liquidity: no option chain")
            result.sources["chain"] = contracts_status.source
            return result
        result.sources["chain"] = f"{contracts_status.source} (indicative feed: derived quotes, trades 15 min delayed)"
        liquid, nearest = pick_contract(contracts, kind, float(o["target_delta"]), int(o["min_open_interest"]), float(o["max_spread_pct"]))
        contract = liquid or nearest
        if contract is None:
            result.failed.append(f"liquidity: no {kind} contracts {o['dte_min']}-{o['dte_max']} days out with a delta")
            return result
        result.contract = contract
        r = float(rate.value) / 100.0 if rate.ok else 0.0
        result.sources["rate"] = f"{rate.source} {rate.value:.2f}% as of {rate.as_of.date()}" if rate.ok else f"risk-free rate missing ({rate.note}); 0% used"
        dte = (contract.expiry - now.date()).days
        T = max(dte, 1) / 365.0
        mid = contract.mid
        iv, iv_source = contract.iv, "feed"
        if iv is None and mid is not None:
            iv = bs.implied_vol(kind, mid, price, contract.strike, T, r)
            iv_source = "Black-Scholes solved from the mid"
        if iv is None:
            result.notes.append("no implied volatility from the feed and none solvable from the mid")
        greeks = dict(contract.greeks)
        greeks_source = "feed"
        if iv is not None and not greeks:
            greeks = {
                "delta": bs.delta(kind, price, contract.strike, T, r, iv), "gamma": bs.gamma(price, contract.strike, T, r, iv),
                "vega": bs.vega(price, contract.strike, T, r, iv), "theta": bs.theta(kind, price, contract.strike, T, r, iv),
            }
            greeks_source = "computed with Black-Scholes"
        m = result.metrics
        m.update({"dte": dte, "T": T, "mid": mid, "ask": contract.ask, "bid": contract.bid, "spread_pct": contract.spread_pct,
                  "open_interest": contract.open_interest, "iv": iv, "iv_source": iv_source, "greeks": greeks, "greeks_source": greeks_source,
                  "rate": r})
        m["vol_ratio"] = (iv / hv60) if (iv is not None and hv60) else None
        rank, n_days = iv_rank
        m["iv_rank"], m["iv_days"] = rank, n_days
        if iv is not None:
            m["expected_move"] = bs.expected_move(price, iv, dte)
            m["expected_move_pct"] = iv * math.sqrt(dte / 365.0) * 100.0
        if contract.ask is not None and contract.ask > 0:
            be = bs.breakeven(kind, contract.strike, contract.ask)
            m["breakeven"] = be
            m["needed_move_pct"] = abs(be - price) / price * 100.0
            m["max_loss"] = contract.ask * 100.0
            if iv is not None:
                m["pop_market"] = bs.prob_beyond(kind, price, be, T, iv, drift=r) * 100.0
            if hv60:
                m["pop_model"] = bs.prob_beyond(kind, price, be, T, hv60, drift=0.0) * 100.0
                m["expected_value"] = bs.expected_value(kind, price, contract.strike, T, hv60, contract.ask, int(o["simulations"]), int(o["seed"]))
                m["simulations"] = int(o["simulations"])
        else:
            result.notes.append("no ask price on the contract")

        # step 4: the checks, in the spec's order
        checks = result.checks
        if direction.value is None:
            checks["direction"] = (False, direction.note)
        elif direction.side is None:
            checks["direction"] = (False, f"z {direction.value:+.2f} is inside +-{o['direction_threshold']:.1f}")
        else:
            checks["direction"] = (True, f"z {direction.value:+.2f}")
        if m["vol_ratio"] is None:
            checks["vol ratio"] = (False, "IV or HV60 missing")
        else:
            checks["vol ratio"] = (m["vol_ratio"] <= float(o["max_vol_ratio"]), f"IV/HV60 {m['vol_ratio']:.2f} (limit {o['max_vol_ratio']})")
        if "needed_move_pct" in m and "expected_move_pct" in m:
            checks["move"] = (m["needed_move_pct"] <= m["expected_move_pct"], f"needs {m['needed_move_pct']:.1f}%, expected {m['expected_move_pct']:.1f}%")
        else:
            checks["move"] = (False, "breakeven or expected move missing")
        liquid_ok = liquid is not None
        checks["liquidity"] = (liquid_ok, f"OI {contract.open_interest}, spread {contract.spread_pct:.1f}% of mid" if contract.spread_pct is not None else f"OI {contract.open_interest}, no two-sided quote")
        if not liquid_ok:
            result.notes.append(f"no {kind} met OI >= {o['min_open_interest']} and spread <= {o['max_spread_pct']}%; showing the nearest-delta contract")
        result.failed = [f"{name}: {detail}" for name, (ok, detail) in checks.items() if not ok]
        if not result.failed:
            result.label = CALL_CANDIDATE if kind == CALL else PUT_CANDIDATE
        if earnings.ok:
            try:
                e_date = date.fromisoformat(str(earnings.value))
                m["earnings_date"] = e_date.isoformat()
                if now.date() <= e_date <= contract.expiry:
                    result.warnings.append(f"earnings {e_date} fall before the {contract.expiry} expiry: implied volatility usually drops right after earnings")
            except ValueError:
                result.notes.append(f"earnings date unreadable: {earnings.value!r}")
        else:
            result.notes.append(f"next earnings date: {earnings.note}")
        return result


# ----- text -----

def _pct(x: Optional[float], digits: int = 1) -> str:
    return "--" if x is None else f"{x:.{digits}f}%"


def _money(x: Optional[float]) -> str:
    return "--" if x is None else f"${x:,.2f}"


def options_summary(res: Optional[OptionsResult], validity: str = "UNPROVEN 0/30") -> str:
    if res is None:
        return "No options check yet."
    m = res.metrics
    lines = [f"[bold]{res.ticker}  {res.label}[/]  validity {validity}"]
    d = res.direction
    lines.append(f"direction z {d.value:+.2f} (needs beyond +-{d.threshold:.1f})" if d.value is not None else f"direction: {d.note}")
    c = res.contract
    if c is not None:
        lines.append(f"{c.symbol}  {c.kind} {c.strike:g} exp {c.expiry} ({m.get('dte')}d)  bid {_money(c.bid)} ask {_money(c.ask)}  OI {c.open_interest}")
        lines.append(f"IV {_pct((m.get('iv') or 0) * 100 if m.get('iv') is not None else None)} ({m.get('iv_source')})  HV20 {_pct((m['hv20'] or 0) * 100 if m.get('hv20') is not None else None)}  HV60 {_pct((m['hv60'] or 0) * 100 if m.get('hv60') is not None else None)}  vol ratio {m['vol_ratio']:.2f}" if m.get("vol_ratio") is not None else f"IV {_pct(m['iv'] * 100) if m.get('iv') is not None else '--'}  vol ratio --")
        rank = m.get("iv_rank")
        lines.append(f"IV rank {rank * 100:.0f}% ({m.get('iv_days')} days)" if rank is not None else f"IV rank collecting, {m.get('iv_days', 0)}/252 days")
        if "expected_move" in m:
            lines.append(f"expected move {_money(m['expected_move'])} ({_pct(m['expected_move_pct'])})  breakeven {_money(m.get('breakeven'))} needs {_pct(m.get('needed_move_pct'))}")
        if "pop_market" in m or "pop_model" in m:
            lines.append(f"P(profit) market {_pct(m.get('pop_market'))}  model {_pct(m.get('pop_model'))}  EV {_money(m.get('expected_value'))}/contract  max loss {_money(m.get('max_loss'))}")
        g = m.get("greeks") or {}
        if g:
            lines.append("greeks " + "  ".join(f"{k} {v:+.3f}" for k, v in g.items() if k in ("delta", "gamma", "theta", "vega")) + f"  ({m.get('greeks_source')})")
    for name, (ok, detail) in res.checks.items():
        lines.append(f"[{'green' if ok else 'red'}]{'pass' if ok else 'FAIL'}[/] {name}: {detail}")
    for w in res.warnings:
        lines.append(f"[yellow]warning: {w}[/]")
    for n in res.notes:
        lines.append(f"[yellow]{n}[/]")
    return "\n".join(lines)


def options_explain(res: OptionsResult, validity: str = "UNPROVEN 0/30") -> str:
    m = res.metrics
    o_lines = [
        f"[bold]Call / put check for {res.ticker}[/]  {res.label}  validity {validity}",
        "",
        "[bold]Step 1, direction[/]: average of three z-scores against the universe: 63-day return, price / 50-day average - 1, 7-day DIRECT news sentiment.",
    ]
    for name, comp in res.direction.components.items():
        if comp.get("z") is None:
            o_lines.append(f"  {name}: missing ({comp.get('note', '')})")
        else:
            o_lines.append(f"  {name} = {comp['raw']:+.4f}   universe mean {comp['mean']:+.4f}, std {comp['std']:.4f} (n={comp['n']})   z {comp['z']:+.2f}")
    if res.direction.value is not None:
        o_lines.append(f"  direction = {res.direction.value:+.2f}; beyond +{res.direction.threshold:.1f} looks at calls, beyond -{res.direction.threshold:.1f} at puts")
    else:
        o_lines.append(f"  direction missing: {res.direction.note}")
    o_lines.append("")
    c = res.contract
    o_lines.append("[bold]Step 2, contract[/]: 30 to 60 days to expiry, delta nearest 0.50, open interest 500 or more, bid-ask spread at most 10% of the mid.")
    if c is None:
        o_lines.append("  none chosen: " + "; ".join(res.failed))
    else:
        o_lines.append(f"  {c.symbol}: {c.kind} strike {c.strike:g}, expiry {c.expiry} ({m.get('dte')} days), delta {c.delta:+.3f}" if c.delta is not None else f"  {c.symbol}")
        o_lines.append(f"  bid {_money(c.bid)}, ask {_money(c.ask)}, mid {_money(c.mid)}, spread {_pct(c.spread_pct)} of mid, open interest {c.open_interest}")
        if c.quote_at:
            o_lines.append(f"  quote time {c.quote_at.isoformat()}")
    o_lines.append("")
    o_lines.append("[bold]Step 3, the numbers[/]")
    if c is not None:
        hv20, hv60, iv = m.get("hv20"), m.get("hv60"), m.get("iv")
        o_lines.append("  realized volatility = standard deviation of daily log returns x sqrt(252): " + f"HV20 {_pct(hv20 * 100) if hv20 is not None else '--'}, HV60 {_pct(hv60 * 100) if hv60 is not None else '--'}")
        o_lines.append(f"  implied volatility {_pct(iv * 100) if iv is not None else '--'} ({m.get('iv_source')})")
        o_lines.append(f"  vol ratio = IV / HV60 = {m['vol_ratio']:.2f} (above 1.3 expensive, below 1.0 cheap)" if m.get("vol_ratio") is not None else "  vol ratio: missing")
        rank = m.get("iv_rank")
        o_lines.append(f"  IV rank = (IV now - 1-year low) / (1-year high - 1-year low) = {rank * 100:.0f}% over {m.get('iv_days')} stored days" if rank is not None else f"  IV rank: collecting one ATM IV a day, {m.get('iv_days', 0)}/252")
        if "expected_move" in m:
            o_lines.append(f"  expected move = price x IV x sqrt(days / 365) = {_money(m['expected_move'])} ({_pct(m['expected_move_pct'])})")
        if "breakeven" in m:
            sign = "+" if c.kind == CALL else "-"
            o_lines.append(f"  breakeven = strike {sign} ask = {c.strike:g} {sign} {c.ask:.2f} = {_money(m['breakeven'])}, a {_pct(m['needed_move_pct'])} move")
        if "pop_market" in m:
            o_lines.append(f"  probability of profit, market's view (lognormal, IV, drift = rate {m['rate'] * 100:.2f}%): {_pct(m['pop_market'])}")
        if "pop_model" in m:
            o_lines.append(f"  probability of profit, model's view (lognormal, HV60, zero drift): {_pct(m['pop_model'])}")
        if "expected_value" in m:
            o_lines.append(f"  expected value = average payoff over {m.get('simulations', 0):,} simulated end prices (HV60, zero drift) - ask, x 100 = {_money(m['expected_value'])} per contract")
        if "max_loss" in m:
            o_lines.append(f"  max loss = ask x 100 = {_money(m['max_loss'])}")
        g = m.get("greeks") or {}
        if g:
            o_lines.append("  greeks (" + str(m.get("greeks_source")) + "): " + ", ".join(f"{k} {v:+.4f}" for k, v in g.items()))
    o_lines.append("")
    o_lines.append("[bold]Step 4, label[/]: Call or Put candidate only if direction passes, vol ratio is 1.2 or less, the needed move is within the expected move, and liquidity passes.")
    for name, (ok, detail) in res.checks.items():
        o_lines.append(f"  {'pass' if ok else 'FAIL'} {name}: {detail}")
    o_lines.append(f"  -> {res.label}")
    for w in res.warnings:
        o_lines.append(f"  warning: {w}")
    if res.notes:
        o_lines.append("")
        o_lines.append("[yellow]Missing or limited:[/]")
        o_lines.extend(f"  {n}" for n in res.notes)
    o_lines.append("")
    o_lines.append("[dim]sources: " + "; ".join(f"{k}: {v}" for k, v in res.sources.items()) + "[/]")
    return "\n".join(o_lines)
