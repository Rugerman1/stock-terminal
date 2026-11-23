from datetime import date, datetime, timedelta, timezone

from stock_terminal.datapoint import FRESH, DataPoint
from stock_terminal.quant.options import (
    CALL, CALL_CANDIDATE, NO_TRADE, PUT, PUT_CANDIDATE, Contract, DirectionScore, OptionsCheck, contracts_from_payload, options_explain,
    options_summary, pick_contract,
)

NOW = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
EXPIRY = date(2027, 1, 8)   # 49 days out


def closes(n=300, start=100.0, daily_vol=0.016, seed=3):
    """A random walk with about 25% annual volatility, ending near the start price so the strikes make sense."""
    import random
    rng = random.Random(seed)
    first = date(2025, 10, 1)
    out, price = [], start
    for i in range(n):
        price *= 1.0 + rng.gauss(0.0, daily_vol)
        out.append(((first + timedelta(days=i)).isoformat(), price))
    scale = start / out[-1][1]
    return [(d, p * scale) for d, p in out]


def contract(symbol="AAPL270108C00100000", kind=CALL, strike=100.0, oi=1200, bid=4.0, ask=4.3, iv=0.25, delta=0.52):
    return Contract(symbol, kind, strike, EXPIRY, oi, bid, ask, iv, delta, {"delta": delta, "gamma": 0.03, "theta": -0.05, "vega": 0.12})


def direction(value, threshold=1.0):
    comps = {"63-day return": {"raw": 0.1, "z": value, "mean": 0.0, "std": 0.1, "n": 10, "note": ""}}
    return DirectionScore(value, comps, threshold)


def test_contracts_from_payload_joins_chain_and_reads_open_interest():
    payload = [
        {"symbol": "X1", "expiration_date": "2027-01-08", "strike_price": 100, "type": "call", "open_interest": "734"},
        {"symbol": "X2", "expiration_date": "2027-01-08", "strike_price": 105, "type": "call", "open_interest": None},
        {"symbol": "bad", "expiration_date": "nope", "strike_price": 1, "type": "call"},
    ]
    chain = {"X1": {"bid": 4.0, "ask": 4.4, "iv": 0.26, "greeks": {"delta": 0.51, "gamma": None}, "quote_at": "2026-11-20T21:00:00+00:00"}}
    out = contracts_from_payload(payload, chain)
    assert [c.symbol for c in out] == ["X1", "X2"]
    x1, x2 = out
    assert x1.open_interest == 734 and x1.mid == 4.2 and abs(x1.spread_pct - 0.4 / 4.2 * 100) < 1e-9 and x1.delta == 0.51 and x1.greeks == {"delta": 0.51}
    assert x1.quote_at.year == 2026
    assert x2.open_interest == 0 and x2.mid is None and x2.spread_pct is None and x2.delta is None


def test_pick_contract_prefers_liquid_nearest_delta():
    a = contract("A", delta=0.62, oi=2000)
    b = contract("B", delta=0.49, oi=100)                 # nearest delta but thin
    c = contract("C", delta=0.45, oi=900, bid=4.0, ask=5.0)  # wide spread: 22%
    d = contract("D", delta=0.40, oi=900)
    liquid, nearest = pick_contract([a, b, c, d], CALL, 0.5, 500, 10.0)
    assert nearest is b and liquid is d                   # a is 0.12 away, d is 0.10 away
    assert pick_contract([a, b], PUT, 0.5, 500, 10.0) == (None, None)


def run(direction_value, cont=None, rate_ok=True, earnings="2027-01-27", contracts_ok=True, cfg_overrides=None, closes_n=300):
    cfg = {"options": {"dte_min": 30, "dte_max": 60, "target_delta": 0.5, "min_open_interest": 500, "max_spread_pct": 10, "direction_threshold": 1.0,
                       "max_vol_ratio": 1.2, "simulations": 4000, "seed": 7, "strike_window_pct": 25, "rate_series": "DGS3MO"}}
    if cfg_overrides:
        cfg["options"].update(cfg_overrides)
    check = OptionsCheck(cfg)
    rate = DataPoint(4.34, "fred/DGS3MO", FRESH, as_of=NOW) if rate_ok else DataPoint.missing("fred/DGS3MO", "no FRED key in .env")
    earn = DataPoint(earnings, "finnhub/earnings-calendar", FRESH, as_of=NOW) if earnings else DataPoint.missing("finnhub/earnings-calendar", "no date listed")
    status = DataPoint([], "alpaca/option-contracts", FRESH) if contracts_ok else DataPoint.missing("alpaca/option-contracts", "no Alpaca key in .env")
    return check.run("AAPL", 100.0, closes(closes_n), direction(direction_value), [cont] if cont else [], status, rate, earn, (None, 3), NOW)


def test_call_candidate_when_every_check_passes():
    res = run(1.4, contract())
    m = res.metrics
    assert res.side == CALL and res.contract.symbol == "AAPL270108C00100000"
    assert m["dte"] == 49 and m["iv"] == 0.25 and m["iv_source"] == "feed" and m["greeks_source"] == "feed"
    assert m["hv60"] is not None and m["vol_ratio"] == 0.25 / m["hv60"]
    assert abs(m["expected_move"] - 100 * 0.25 * (49 / 365) ** 0.5) < 1e-9
    assert m["breakeven"] == 104.3 and abs(m["needed_move_pct"] - 4.3) < 1e-9 and m["max_loss"] == 430.0
    assert 0 < m["pop_market"] < 100 and 0 < m["pop_model"] < 100 and isinstance(m["expected_value"], float)
    assert m["iv_days"] == 3 and m["iv_rank"] is None
    assert all(ok for ok, _ in res.checks.values()), res.checks
    assert res.label == CALL_CANDIDATE and res.failed == []
    assert res.warnings == []                                           # earnings Jan 27 is after the Jan 8 expiry
    assert "indicative feed" in res.sources["chain"] and "4.34%" in res.sources["rate"]


def test_put_side_breakeven_and_earnings_warning():
    put = contract("AAPL270108P00100000", kind=PUT, bid=3.8, ask=4.0, delta=-0.48)
    res = run(-1.3, put, earnings="2026-12-15")
    assert res.side == PUT and res.metrics["breakeven"] == 96.0 and res.label == PUT_CANDIDATE
    assert res.warnings and "earnings 2026-12-15 fall before the 2027-01-08 expiry" in res.warnings[0]


def test_no_trade_names_the_failed_check():
    inside = run(0.4, contract())
    assert inside.label == NO_TRADE and inside.failed[0].startswith("direction: z +0.40 is inside +-1.0")
    assert inside.contract is not None and inside.metrics["vol_ratio"] is not None   # the ATM call is still analysed for IV and HV

    expensive = run(1.4, contract(iv=0.80))
    assert expensive.label == NO_TRADE and any(f.startswith("vol ratio: IV/HV60") for f in expensive.failed)

    thin = run(1.4, contract(oi=50))
    assert thin.label == NO_TRADE and any(f.startswith("liquidity: OI 50") for f in thin.failed)
    assert any("showing the nearest-delta contract" in n for n in thin.notes)

    far = run(1.4, contract(strike=100.0, bid=20.0, ask=21.0))          # breakeven 121 needs a 21% move, expected is under 10%
    assert far.label == NO_TRADE and any(f.startswith("move: needs 21.0%") for f in far.failed)

    missing_dir = DirectionScore(None, {}, 1.0, "only 1 of 3 components available; needs 2")
    cfg = {"options": {"dte_min": 30, "dte_max": 60, "target_delta": 0.5, "min_open_interest": 500, "max_spread_pct": 10, "direction_threshold": 1.0, "max_vol_ratio": 1.2, "simulations": 100, "seed": 1, "strike_window_pct": 25, "rate_series": "x"}}
    res = OptionsCheck(cfg).run("AAPL", 100.0, closes(), missing_dir, [contract()], DataPoint([], "s", FRESH), DataPoint.missing("f", "x"), DataPoint.missing("e", "y"), (None, 0), NOW)
    assert res.label == NO_TRADE and res.failed[0] == "direction: only 1 of 3 components available; needs 2"
    assert "0% used" in res.sources["rate"] and any(n.startswith("next earnings date:") for n in res.notes)


def test_iv_solved_from_mid_and_greeks_computed_when_the_feed_has_none():
    bare = Contract("Z", CALL, 100.0, EXPIRY, 900, 4.0, 4.3, None, None, {})
    bare.delta = 0.5                                                     # pick_contract needs a delta; the Greeks block stays empty
    res = run(1.4, bare)
    m = res.metrics
    assert m["iv_source"] == "Black-Scholes solved from the mid" and 0.1 < m["iv"] < 1.0
    assert m["greeks_source"] == "computed with Black-Scholes" and set(m["greeks"]) == {"delta", "gamma", "vega", "theta"}


def test_missing_chain_and_short_history_are_named():
    res = run(1.4, None, contracts_ok=False)
    assert res.label == NO_TRADE and res.contract is None and "liquidity: no option chain" in res.failed and "option chain: no Alpaca key in .env" in res.notes
    short = run(1.4, contract(), closes_n=40)
    assert short.metrics["hv60"] is None and any("HV60 needs 61" in n for n in short.notes) and short.label == NO_TRADE


def test_summary_and_explain_text():
    res = run(1.4, contract())
    summary = options_summary(res)
    assert "AAPL  Call candidate" in summary and "direction z +1.40" in summary and "IV rank collecting, 3/252 days" in summary and "[green]pass[/] liquidity: OI 1200" in summary
    text = options_explain(res)
    assert "Step 1, direction" in text and "Step 4, label" in text and "breakeven = strike + ask = 100 + 4.30" in text and "-> Call candidate" in text
    assert options_summary(None) == "No options check yet."
