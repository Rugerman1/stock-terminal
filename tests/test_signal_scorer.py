from datetime import date, datetime, timedelta, timezone

from stock_terminal.quant.signal_scorer import SignalScorer, outcome_for, session_n_after, session_on_or_before, window_for
from stock_terminal.quant.store import LONG_TERM, OPTIONS, MarketStore

UTC = timezone.utc
FIRST = date(2026, 1, 5)   # a Monday


def sessions(n):
    """n weekday session dates from FIRST."""
    out, day = [], FIRST
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def seed(store, n=120):
    days = sessions(n)
    store.upsert_bars("SPY", [(d, 100.0 + 0.1 * i) for i, d in enumerate(days)])            # SPY drifts up 0.1 a session
    store.upsert_bars("AAPL", [(d, 100.0 + 0.3 * i) for i, d in enumerate(days)])           # AAPL beats it
    store.upsert_bars("MSFT", [(d, 100.0 - 0.05 * i) for i, d in enumerate(days)])          # MSFT lags
    store.upsert_bars("U1", [(d, 100.0 + 0.2 * i) for i, d in enumerate(days)])
    store.upsert_bars("U2", [(d, 100.0) for d in days])
    return days


def at(day_iso, hour=21):
    return datetime.fromisoformat(day_iso).replace(hour=hour, tzinfo=UTC)


def test_session_helpers():
    closes = [("2026-01-05", 1.0), ("2026-01-06", 2.0), ("2026-01-08", 3.0)]
    assert session_on_or_before(closes, date(2026, 1, 7)) == ("2026-01-06", 2.0)
    assert session_on_or_before(closes, date(2026, 1, 4)) is None
    assert session_n_after(closes, date(2026, 1, 5), 2) == ("2026-01-08", 3.0) and session_n_after(closes, date(2026, 1, 5), 3) is None


def test_outcomes_long_term_and_options():
    assert outcome_for(LONG_TERM, 1, 100, 110, 100, 105, None)[0] == 1       # beat SPY, long side
    assert outcome_for(LONG_TERM, 1, 100, 103, 100, 105, None)[0] == 0       # rose, but less than SPY
    assert outcome_for(LONG_TERM, -1, 100, 103, 100, 105, None)[0] == 1      # short side wins when it lags
    assert outcome_for(LONG_TERM, 1, 100, 110, None, None, None)[0] is None
    assert outcome_for(OPTIONS, 1, 100, 110, None, None, 108.0)[0] == 1 and outcome_for(OPTIONS, 1, 100, 107, None, None, 108.0)[0] == 0
    assert outcome_for(OPTIONS, -1, 100, 90, None, None, 94.0)[0] == 1
    raw = outcome_for(OPTIONS, 1, 100, 101, None, None, None)
    assert raw[0] == 1 and abs(raw[1] - 0.01) < 1e-12 and raw[3] == "no breakeven stored; raw direction"


def test_long_term_signal_scores_at_the_horizon_against_spy(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    days = seed(store)
    store.record_label("AAPL", "Favorable", 100.0, at(days[4]), 63)
    store.record_label("MSFT", "Favorable", 100.0, at(days[4]), 63)
    store.record_label("U2", "Neutral", 100.0, at(days[4]), 63)
    scorer = SignalScorer(store, ["AAPL", "MSFT", "U1", "U2"])
    events = scorer.score_due(at(days[-1]))
    assert len(events) == 2
    aapl, msft = (s for s in store.scored_signals(LONG_TERM))
    assert aapl.ticker == "AAPL" and aapl.outcome == 1 and aapl.scored_at.date().isoformat() == days[4 + 63] and aapl.score_note.startswith("horizon; vs SPY")
    assert msft.outcome == 0 and msft.bench_return > msft.stock_return
    neutral = [s for s in store.signals("U2")][0]
    assert neutral.outcome is None and neutral.scored_at is not None and "Neutral" in neutral.score_note
    assert store.unscored_signals() == []


def test_pending_until_the_market_gets_there_and_early_close_scores_at_the_change(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    days = seed(store, n=40)
    store.record_label("AAPL", "Favorable", 100.0, at(days[4]), 63)
    scorer = SignalScorer(store, ["AAPL"])
    assert scorer.score_due(at(days[-1])) == []                             # only 35 sessions after the entry: pending
    assert window_for(store.open_signal("AAPL"), store.closes("AAPL")) is None
    store.record_label("AAPL", "Neutral", 104.0, at(days[20]), 63)          # the label changed on session 20: the bet closed
    events = SignalScorer(store, ["AAPL"]).score_due(at(days[-1]))
    assert len(events) == 1 and "closed early" in events[0] and days[20] in events[0]


def test_options_signal_scores_at_expiry_against_breakeven(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    days = seed(store)
    store.record_label("AAPL", "Call candidate", 101.2, at(days[4]), 30, kind=OPTIONS, detail="AAPLC100", target=105.0)
    store.record_label("MSFT", "Put candidate", 99.8, at(days[4]), 30, kind=OPTIONS, detail="MSFTP100", target=97.0)
    scorer = SignalScorer(store, ["AAPL", "MSFT", "U1", "U2"])
    assert scorer.score_due(at(days[10])) == []                            # 30 calendar days have not passed
    events = scorer.score_due(at(days[-1]))
    assert len(events) == 2
    call, put = store.scored_signals(OPTIONS)
    expiry = (datetime.fromisoformat(days[4]).date() + timedelta(days=30)).isoformat()
    assert call.outcome == 1 and call.scored_at.date().isoformat() <= expiry and "vs breakeven 105.00" in call.score_note
    assert put.outcome == 0                                                 # MSFT drifted down, but not under 97


def test_universe_outcomes_and_gap_outcome(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    days = seed(store)
    scorer = SignalScorer(store, ["AAPL", "MSFT", "U1", "U2", "SPY"])
    pool = scorer.universe_outcomes(LONG_TERM, 1, days[4], days[67])
    assert pool == [1, 0, 1, 0]                                              # AAPL and U1 beat SPY; MSFT and U2 do not; SPY itself is left out
    assert scorer.universe_outcomes(OPTIONS, 1, days[4], days[67], target_ratio=1.02) == [1, 0, 1, 0]
    assert scorer.gap_outcome("AAPL", -1, at(days[10]), at(days[30])) == 0  # a short test trade on AAPL loses while it outruns SPY
    assert scorer.gap_outcome("AAPL", 1, at(days[10]), at(days[30])) == 1
    assert scorer.gap_outcome("AAPL", 1, at(days[10]), at(days[10])) is None
