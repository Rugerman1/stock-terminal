from statistics import mean, stdev

from stock_terminal.quant.factors import FactorValue
from stock_terminal.quant.scoring import FAVORABLE, NEUTRAL, UNFAVORABLE, RawFactors, explain_text, raw_text, score_all, score_summary

WEIGHTS = {"momentum": 0.30, "trend": 0.10, "value": 0.20, "quality": 0.25, "low_risk": 0.10, "news": 0.05}


def raw(symbol: str, mom: float, roe: float, news=None, as_of="2026-11-20", price=100.0) -> RawFactors:
    return RawFactors(
        symbol=symbol, as_of=as_of, price=price,
        values={
            "momentum": FactorValue("momentum", mom),
            "trend": FactorValue("trend", 0.0),
            "low_risk": FactorValue("low_risk", -0.2),
            "value": FactorValue("value", 0.03),
            "news": FactorValue("news", news) if news is not None else FactorValue("news", None, "needs 5 scored DIRECT headlines, have 0"),
        },
        quality={
            "roe": FactorValue("roe", roe),
            "gross_margin": FactorValue("gross_margin", 40.0),
            "neg_debt_to_equity": FactorValue("neg_debt_to_equity", -1.0),
        },
        sources={"momentum": "bars", "value": "finnhub", "quality": "finnhub", "news": "store"},
    )


def universe():
    members = [f"U{i}" for i in range(10)]
    raws = {m: raw(m, float(i), 10.0 + i) for i, m in enumerate(members)}
    return members, raws


def test_zscores_labels_and_percentiles():
    members, raws = universe()
    scores = score_all(raws, members, WEIGHTS)
    values = list(range(10))
    sd = stdev(values)
    z9 = (9 - mean(values)) / sd
    top = scores["U9"]
    assert abs(top.factor("momentum").z - z9) < 1e-12
    assert abs(top.factor("quality").z - z9) < 1e-12               # ROE has the same spread, so the same z; the other parts have no spread
    assert top.factor("quality").note == "1 of 3 parts available"
    assert abs(top.composite - 0.55 * z9) < 1e-12
    assert top.label == FAVORABLE and top.percentile == 90.0 and top.scored_n == 10 and top.universe_n == 10
    assert scores["U8"].label == FAVORABLE and scores["U7"].label == NEUTRAL
    assert scores["U1"].label == UNFAVORABLE and scores["U0"].label == UNFAVORABLE and scores["U0"].percentile == 0.0
    assert top.favorable_min == scores["U8"].composite and top.unfavorable_max == scores["U1"].composite
    notes = " | ".join(top.notes)
    assert "trend missing (no universe spread (n=10)), counted as z = 0" in notes and "news missing (needs 5" in notes


def test_watchlist_name_outside_the_universe_is_scored_against_it():
    members, raws = universe()
    raws["ZZZ"] = raw("ZZZ", 20.0, 30.0)
    scores = score_all(raws, members, WEIGHTS)
    zzz = scores["ZZZ"]
    assert zzz.factor("momentum").z == 3.0 and zzz.factor("quality").z == 3.0   # capped
    assert abs(zzz.composite - 0.55 * 3.0) < 1e-12 and zzz.label == FAVORABLE and zzz.percentile == 100.0
    assert not zzz.in_universe and scores["U9"].scored_n == 10                   # the outsider does not change the universe stats


def test_small_universe_gives_no_labels():
    members = ["A", "B", "C"]
    raws = {m: raw(m, float(i), 10.0 + i) for i, m in enumerate(members)}
    scores = score_all(raws, members, WEIGHTS)
    assert scores["A"].composite is not None and scores["A"].label is None
    assert any("universe too small" in n for n in scores["A"].notes)


def test_nothing_scorable_gives_no_composite():
    r = RawFactors("X", None)
    scores = score_all({"X": r}, ["X"], WEIGHTS)
    assert scores["X"].composite is None and scores["X"].label is None and len(scores["X"].notes) == 6


def test_summary_and_explain_text():
    members, raws = universe()
    raws["U9"].values["news"] = FactorValue("news", 0.4, inputs={"headlines": 7, "halflife_days": 3, "sum_of_weights": 3.1, "newest_age_days": 0.2})
    for i in range(9):
        raws[f"U{i}"].values["news"] = FactorValue("news", 0.1 * i - 0.4, inputs={"headlines": 5, "halflife_days": 3, "sum_of_weights": 2.0, "newest_age_days": 1.0})
    scores = score_all(raws, members, WEIGHTS)
    summary = score_summary(scores["U9"])
    assert "U9" in summary and "Favorable" in summary and "composite" in summary and "momentum" in summary and "90th pct of 10" in summary
    text = explain_text(scores["U9"], "Test universe")
    assert "Composite = sum of weight x z" in text and "Test universe, 10 of 10 names scored" in text
    assert "price 21 trading days ago / price 252 trading days ago - 1" in text
    assert "7 scored DIRECT headlines" in text and "source: bars" in text and "-> Favorable" in text
    assert raw_text("momentum", 0.1234) == "+12.3%" and raw_text("roe", 127.61) == "127.6%" and raw_text("news", 0.4) == "+0.40" and raw_text("x", None) == "--"
    assert score_summary(None) == "No score yet."
