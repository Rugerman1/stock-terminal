"""Phase 6: the backtest, the experiment log, sizing, the calendar and the brief."""
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

from stock_terminal.datapoint import FRESH, DataPoint
from stock_terminal.quant.backtest import backtest_summary, moments, params_for, run_backtest
from stock_terminal.quant.brief import build_brief, write_brief
from stock_terminal.quant.calendar import Event, dates_strip, load_events, upcoming
from stock_terminal.quant.experiments import EULER, deflated_sharpe, expected_max_sharpe, experiments_summary, probabilistic_sharpe
from stock_terminal.quant.sizing import atr, contracts_for, correlation, shares_for, sizing_summary, true_ranges
from stock_terminal.quant.store import MarketStore

UTC = timezone.utc
REPO = Path(__file__).resolve().parent.parent


def sessions(n, first=date(2024, 1, 1)):
    out, day = [], first
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def seed_universe(store, n=600):
    """Eight names with different drifts so the replay has winners and losers, plus SPY in the middle."""
    days = sessions(n)
    names = ["U0", "U1", "U2", "U3", "U4", "U5", "U6", "U7"]
    for i, name in enumerate(names):
        growth = 1.0 + 0.0002 * (i - 3.5)
        store.upsert_bars(name, [(d, 100.0 * growth ** k, 100.0 * growth ** k * 1.01, 100.0 * growth ** k * 0.99) for k, d in enumerate(days)])
    store.upsert_bars("SPY", [(d, 100.0 * 1.0001 ** k) for k, d in enumerate(days)])
    return names, days


# ----- backtest -----

def test_backtest_replays_price_factors_and_feeds_the_tests(tmp_path, cfg):
    store = MarketStore(tmp_path / "m.db")
    names, days = seed_universe(store)
    cfg["backtest"]["history_days"] = 2000
    progress = []
    result = run_backtest(store, names + ["SPY"], cfg, date.fromisoformat(days[-1]), progress.append)
    assert result.rebalances >= 4 and result.signals > 0 and result.report is not None
    # Persistent drifts: the top names keep beating SPY and the bottom names keep lagging, so the replay wins most of the time.
    assert result.wins / result.signals > 0.7 and result.report.binomial.probability < 0.05
    assert result.sharpe is not None and result.sharpe > 0 and result.skew is not None
    assert set(result.excluded) == {"value", "quality", "news"} and result.factors_used == ["momentum", "trend", "low_risk"]
    assert any("survivorship" in n for n in result.notes) and progress
    assert result.start == days[cfg["backtest"]["min_history_sessions"]]
    d = result.to_dict()
    assert d["status"] == result.report.status and d["binomial"]["n"] == result.signals and d["bef_tickers"] == len(result.report.bef)
    text = backtest_summary(d | {"run_at": "2026-11-22T22:00:00", "params_hash": "abc"})
    assert "replayed labels" in text and "value left out" in text and "trial abc" in text
    assert backtest_summary(None) == "No backtest yet." and "nothing to replay" in backtest_summary({"signals": 0, "notes": []})


def test_backtest_needs_enough_history(tmp_path, cfg):
    store = MarketStore(tmp_path / "m.db")
    names, days = seed_universe(store, n=200)
    result = run_backtest(store, names + ["SPY"], cfg, date.fromisoformat(days[-1]))
    assert result.signals == 0 and any("needs" in n for n in result.notes)


def test_params_hash_changes_with_a_weight(cfg):
    params, digest = params_for(cfg)
    assert params["weights"]["momentum"] == 0.3 and len(digest) == 12
    cfg["quant"]["weights"]["momentum"] = 0.35
    assert params_for(cfg)[1] != digest


def test_moments():
    mean, sd, skew, kurt = moments([0.01, 0.02, 0.03, 0.04, 0.10])
    assert abs(mean - 0.04) < 1e-12 and sd > 0 and skew > 0 and kurt > 0
    assert moments([0.01, 0.02]) == (None, None, None, None)
    assert moments([0.01, 0.01, 0.01])[2] is None


# ----- experiment log and the Deflated Sharpe Ratio -----

def test_expected_max_sharpe_and_psr():
    z = NormalDist().inv_cdf
    sr0 = expected_max_sharpe([0.5, 1.0])
    expected = math.sqrt(0.125) * ((1 - EULER) * z(1 - 1 / 2) + EULER * z(1 - 1 / (2 * math.e)))
    assert abs(sr0 - expected) < 1e-12
    assert expected_max_sharpe([0.5]) is None and expected_max_sharpe([0.7, 0.7, 0.7]) == 0.0
    assert abs(probabilistic_sharpe(0.0, 0.0, 10, 0.0, 3.0) - 0.5) < 1e-12        # at the benchmark, a coin flip
    assert probabilistic_sharpe(0.5, 0.1, 50, 0.0, 3.0) > 0.95
    assert probabilistic_sharpe(0.5, 0.1, 1, 0.0, 3.0) is None


def test_deflated_sharpe_needs_two_trials_then_reports():
    one = [{"params_hash": "a", "sharpe": 0.4, "n_returns": 12, "skew": 0.1, "kurtosis": 3.2, "at": "2026-11-22T22:00"}]
    r = deflated_sharpe(one, "a")
    assert r.n_trials == 1 and r.sr == 0.4 and r.sr0 is None and r.dsr is None and "two or more" in r.note
    two = one + [{"params_hash": "b", "sharpe": 0.1, "n_returns": 12, "skew": 0.0, "kurtosis": 3.0, "at": "2026-11-22T23:00"}]
    r = deflated_sharpe(two, "a")
    assert r.n_trials == 2 and r.sr0 is not None and 0 < r.dsr < 1
    assert deflated_sharpe(two, "zzz").note.startswith("the current parameter set")
    text = experiments_summary(two, r)
    assert "2 trials" in text and "Deflated Sharpe Ratio" in text and "not tuned" in text


# ----- sizing and correlation -----

def test_true_range_atr_and_sizing():
    bars = [("d0", 100.0, 101.0, 99.0), ("d1", 102.0, 103.0, 100.0), ("d2", 101.0, 104.0, 100.5)]
    assert true_ranges(bars) == [max(3.0, 3.0, 0.0), max(3.5, 2.0, 1.5)]
    assert atr(bars, 2) == 3.25 and atr(bars, 3) is None
    assert atr([("d0", 100.0, None, None), ("d1", 101.0, None, None)], 1) is None
    assert shares_for(10_000, 1.0, 2.0, 2.0) == 25 and shares_for(10_000, 1.0, None, 2.0) is None and shares_for(0, 1.0, 2.0, 2.0) is None
    assert contracts_for(10_000, 1.0, 4.0) == 0 and contracts_for(10_000, 1.0, 0.5) == 2 and contracts_for(10_000, 1.0, None) is None


def test_correlation_table_and_warning():
    days = sessions(120)
    moves = [0.01 * (-1) ** i * (1 + 0.1 * (i % 3)) for i in range(len(days))]   # alternating, not constant
    def series(scale, start=100.0):
        out, price = [], start
        for d, r in zip(days, moves):
            price *= math.exp(scale * r)
            out.append((d, price))
        return out
    up, same, down, flat = series(1.0), series(0.5, 50.0), series(-1.0, 300.0), [(d, 100.0) for d in days]
    corr = correlation({"A": up, "B": same, "C": down}, 90, 0.8)
    assert corr.sessions == 90 and abs(corr.matrix[("A", "B")] - 1.0) < 1e-6 and corr.matrix[("A", "C")] < -0.99
    assert len(corr.warnings) == 1 and corr.warnings[0].startswith("A and B move together")
    assert correlation({"A": up}, 90, 0.8).note.startswith("needs two") and "shared sessions" in correlation({"A": up[:10], "B": same[:10]}, 90, 0.8).note
    still = correlation({"A": up, "F": flat}, 90, 0.8)
    assert still.matrix[("A", "F")] == 0.0 and still.note.startswith("no price variation") and still.warnings == []
    cfg = {"risk_pct": 1.0, "atr_sessions": 14, "atr_multiple": 2.0, "correlation_sessions": 90, "correlation_warn": 0.8}
    from stock_terminal.quant.sizing import SizingRow
    rows = [SizingRow("A", 100.0, 2.0, 4.0, 25, 4.0, 0), SizingRow("B", None, None, None, None, None, None, "ATR needs 15 sessions")]
    text = sizing_summary(10_000, rows, corr, cfg)
    assert "account $10,000" in text and "A      " in text and "ATR needs 15" in text and "move together" in text
    assert "Enter an account size" in sizing_summary(None, rows, corr, cfg)


# ----- calendar -----

def test_shipped_calendar_and_dates_strip():
    events, note = load_events(REPO / "calendar.yaml")
    assert note == "" and events and events == sorted(events, key=lambda e: e.date)
    today = date(2026, 11, 22)
    nxt = upcoming(events, today, 2)
    assert nxt[0].name == "FOMC" and nxt[0].date == date(2026, 12, 8) and nxt[0].end == date(2026, 12, 9)
    assert nxt[1].name == "CPI (Nov)" and nxt[1].date == date(2026, 12, 10)
    earnings = {"AAPL": DataPoint("2027-01-27", "finnhub", FRESH, as_of=datetime(2027, 1, 27, tzinfo=UTC))}
    strip = dates_strip(events, earnings, today, limit=2)
    assert strip == "Dates: FOMC Dec 8-9 (16d)  |  CPI (Nov) Dec 10 (18d)  |  AAPL earnings Jan 27 (66d)"
    assert all(e.source for e in events)                                   # every date names where it was read


def test_missing_calendar_is_a_note(tmp_path):
    events, note = load_events(tmp_path / "nope.yaml")
    assert events == [] and "not found" in note
    assert dates_strip([], {}, date(2026, 11, 22), note=note).startswith("Dates: none") and "not found" in dates_strip([], {}, date(2026, 11, 22), note=note)


# ----- brief -----

def test_brief_is_written_from_the_boards(tmp_path, cfg):
    from stock_terminal.quant.engine import QuantBoard
    from stock_terminal.quant.scoring import FactorScore, Score
    score = Score("AAPL", 0.11, "Neutral", 70.0, [FactorScore("momentum", 0.3, 0.297, 0.08, 0.02)], as_of="2026-11-20", scored_n=101, universe_n=101, price=343.4)
    quant = QuantBoard(scores={"AAPL": score}, status="scored", backtest={"signals": 0, "notes": ["nothing yet"]})
    now = datetime(2026, 11, 22, 22, 30, tzinfo=UTC)
    text = build_brief(cfg, None, None, quant, now, "Dates: FOMC Dec 8-9 (16d)")
    assert text.startswith("# Watchtower brief, Sunday November 22, 2026, 5:30 PM ET") and "## AAPL" in text and "Neutral" in text and "## Validity" in text
    assert "never buy or sell" in text and "Dates: FOMC" in text and "FRED API" in text
    path = write_brief(tmp_path / "briefs", text, now)
    assert path.name == "2026-11-22.md" and path.read_text(encoding="utf-8") == text


# ----- store additions -----

def test_store_bars_backtests_experiments_settings(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    store.upsert_bars("AAPL", [("2026-11-19", 350.0), ("2026-11-20", 343.4, 352.0, 340.0)])
    assert store.bars("AAPL") == [("2026-11-19", 350.0, None, None), ("2026-11-20", 343.4, 352.0, 340.0)]
    assert store.has_ranges("AAPL") and not store.has_ranges("MSFT") and store.first_bar_date("AAPL") == "2026-11-19"
    now = datetime(2026, 11, 22, 22, 0, tzinfo=UTC)
    assert store.latest_backtest() is None
    store.save_backtest(now, {"w": 1}, "h1", {"signals": 3})
    latest = store.latest_backtest()
    assert latest["params_hash"] == "h1" and latest["result"] == {"signals": 3} and latest["run_at"] == now
    assert store.record_experiment(now, "h1", {"w": 1}, 0.3, 10, 0.1, 3.0) is True
    assert store.record_experiment(now, "h1", {"w": 1}, 0.4, 11, 0.1, 3.0) is False       # same trial, Sharpe refreshed
    assert store.record_experiment(now, "h2", {"w": 2}, 0.1, 10, 0.0, 3.0) is True
    trials = store.experiments()
    assert [t["params_hash"] for t in trials] == ["h1", "h2"] and trials[0]["sharpe"] == 0.4
    assert store.setting("sizing:account") is None and store.setting("x", "dflt") == "dflt"
    store.set_setting("sizing:account", "25000.00")
    assert store.setting("sizing:account") == "25000.00"
