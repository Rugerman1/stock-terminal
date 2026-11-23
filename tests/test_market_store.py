from datetime import datetime, timedelta, timezone

from stock_terminal.quant.store import MarketStore

NOW = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)


def test_bars_round_trip_and_ordering(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    assert store.last_bar_date("AAPL") is None and store.closes("AAPL") == []
    assert store.upsert_bars("aapl", [("2026-11-18", 350.0), ("2026-11-20", 343.4), ("2026-11-19", 352.0)]) == 3
    assert store.closes("AAPL") == [("2026-11-18", 350.0), ("2026-11-19", 352.0), ("2026-11-20", 343.4)]
    assert store.closes("AAPL", limit=2) == [("2026-11-19", 352.0), ("2026-11-20", 343.4)]
    assert store.last_bar_date("AAPL") == "2026-11-20"
    store.upsert_bars("AAPL", [("2026-11-20", 344.0)])          # replace, not duplicate
    assert store.closes("AAPL")[-1] == ("2026-11-20", 344.0) and store.bar_counts() == {"AAPL": 3}
    assert store.closes_many(["AAPL", "MSFT"])["MSFT"] == []


def test_scores_latest_and_history(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    assert store.latest_scores() == {} and store.latest_run() is None
    store.save_scores(NOW - timedelta(days=1), [{"symbol": "AAPL", "as_of": "2026-11-19", "composite": 0.5, "label": "Neutral", "percentile": 55.0, "factors": {"momentum": {"z": 0.1}}}])
    store.save_scores(NOW, [{"symbol": "AAPL", "as_of": "2026-11-20", "composite": 1.2, "label": "Favorable", "percentile": 88.0, "factors": {}}])
    latest = store.latest_scores()
    assert latest["AAPL"]["label"] == "Favorable" and latest["AAPL"]["composite"] == 1.2 and store.latest_run() == NOW
    runs = store.score_runs("AAPL")
    assert [r["label"] for r in runs] == ["Neutral", "Favorable"]


def test_signal_log_records_only_label_changes(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    assert store.open_signal("AAPL") is None
    first = store.record_label("aapl", "Neutral", 340.0, NOW - timedelta(days=2))
    assert first is not None and first.ticker == "AAPL" and first.direction == 0 and first.closed_at is None
    assert store.record_label("AAPL", "Neutral", 341.0, NOW - timedelta(days=1)) is None     # same label: nothing logged
    second = store.record_label("AAPL", "Favorable", 343.4, NOW)
    assert second is not None and second.direction == 1 and second.open_price == 343.4 and second.horizon_days == 63
    signals = store.signals("AAPL")
    assert len(signals) == 2
    assert signals[0].closed_at == NOW and signals[0].close_price == 343.4 and signals[0].note == "label changed to Favorable"
    assert store.open_signal("AAPL").id == second.id
    assert store.record_label("MSFT", "Unfavorable", 480.0, NOW).direction == -1
    assert [s.ticker for s in store.signals()] == ["AAPL", "AAPL", "MSFT"] and len(store.signals(kind="options")) == 0


def test_options_signals_change_on_contract_and_close_on_no_trade(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    first = store.record_label("AAPL", "Call candidate", 343.4, NOW, 49, kind="options", detail="AAPL270108C00345000")
    assert first.kind == "options" and first.direction == 1 and first.horizon_days == 49 and first.note == "AAPL270108C00345000"
    assert store.record_label("AAPL", "Call candidate", 344.0, NOW + timedelta(hours=1), 49, kind="options", detail="AAPL270108C00345000") is None
    second = store.record_label("AAPL", "Call candidate", 345.0, NOW + timedelta(days=1), 48, kind="options", detail="AAPL270115C00350000")
    assert second is not None and store.signals("AAPL", "options")[0].closed_at is not None        # same label, new contract
    closed = store.close_open("AAPL", "options", 340.0, NOW + timedelta(days=2), "label changed to No trade")
    assert closed.id == second.id and closed.close_price == 340.0 and closed.note.endswith("label changed to No trade")
    assert store.open_signal("AAPL", "options") is None and store.close_open("AAPL", "options", 1.0, NOW, "x") is None
    assert store.open_signal("AAPL") is None                                                           # long-term log untouched


def test_iv_history_and_rank(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    assert store.iv_rank("AAPL", 0.3) == (None, 0)
    for i in range(10):
        store.record_iv("aapl", f"2026-11-{i + 1:02d}", 0.20 + 0.01 * i, "C1")
    store.record_iv("AAPL", "2026-11-10", 0.29, "C2")                   # same day: replaced, not duplicated
    assert store.iv_rank("AAPL", 0.25) == (None, 10)                     # still collecting
    rank, n = store.iv_rank("AAPL", 0.25, need=5)
    assert n == 10 and abs(rank - (0.25 - 0.20) / (0.29 - 0.20)) < 1e-9
    assert store.iv_rank("AAPL", 0.50, need=5)[0] == 1.0 and store.iv_rank("AAPL", 0.10, need=5)[0] == 0.0
    assert store.iv_rank("AAPL", None, need=5) == (None, 10)


def test_outcome_columns_are_added_to_an_old_database_and_round_trip(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as con:   # the Phase 3 shape of the table, without the scoring columns
        con.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL, direction INTEGER NOT NULL, opened_at TEXT NOT NULL, open_price REAL, horizon_days INTEGER NOT NULL, closed_at TEXT, close_price REAL, outcome INTEGER, note TEXT)")
        con.execute("INSERT INTO signals (ticker, kind, label, direction, opened_at, open_price, horizon_days, note) VALUES ('AAPL', 'long_term', 'Favorable', 1, '2026-10-01T20:00:00+00:00', 250.0, 63, '')")
    store = MarketStore(path)
    old = store.signals("AAPL")[0]
    assert old.target is None and old.scored_at is None and old.score_note == ""
    assert [s.id for s in store.unscored_signals()] == [old.id] and store.open_count("long_term") == 1
    store.save_outcome(old.id, 1, NOW, 260.0, 0.04, 0.01, "horizon; vs SPY")
    scored = store.scored_signals("long_term")[0]
    assert scored.outcome == 1 and scored.score_price == 260.0 and scored.stock_return == 0.04 and scored.bench_return == 0.01 and scored.scored_at == NOW
    assert store.unscored_signals() == [] and store.open_count("long_term") == 0
    options = store.record_label("AAPL", "Call candidate", 250.0, NOW, 40, kind="options", detail="AAPLC", target=262.5)
    assert options.target == 262.5 and store.unscored_signals("options")[0].id == options.id
