from datetime import timedelta

from conftest import NOW
from stock_terminal.news.models import DIRECT, PEER


def test_upsert_marks_duplicates_and_skips_known_ids(store, make_item):
    a = make_item("alpaca:1", "Nvidia shares fall after earnings report", hours_ago=3)
    b = make_item("finnhub:1", "Nvidia shares fall after earnings report - Reuters", hours_ago=2)
    c = make_item("alpaca:2", "Apple unveils a new iPad", hours_ago=1)
    new, dups = store.upsert_items([a, b, c], NOW)
    assert [i.id for i in new] == ["alpaca:1", "alpaca:2"] and dups == 1
    assert store.get("finnhub:1").duplicate_of == "alpaca:1"
    assert store.has("alpaca:1") and store.count() == 3
    new, dups = store.upsert_items([a, b, c], NOW)      # the same batch again: nothing new
    assert new == [] and dups == 0 and store.count() == 3


def test_recent_is_newest_first_and_windowed(store, make_item):
    store.upsert_items([make_item("x:1", "Old story about rates", hours_ago=200), make_item("x:2", "New story about chips", hours_ago=1)], NOW)
    items = store.recent(NOW - timedelta(days=7))
    assert [i.id for i in items] == ["x:2"]
    assert [i.id for i in store.recent(NOW - timedelta(days=30))] == ["x:2", "x:1"]


def test_sentiment_round_trip(store, make_item):
    store.upsert_items([make_item("x:1", "Apple beats estimates"), make_item("x:2", "Nvidia unveils a new chip")], NOW)
    unscored = store.unscored(NOW - timedelta(days=7))
    assert {i.id for i in unscored} == {"x:1", "x:2"}
    store.set_sentiment("x:1", 0.8, "finbert")
    assert [i.id for i in store.unscored(NOW - timedelta(days=7))] == ["x:2"]
    assert store.get("x:1").sentiment == 0.8 and store.get("x:1").sentiment_note == "finbert"


def test_reaction_schedule_due_and_save(store, make_item):
    item = make_item("x:1", "Apple beats estimates", hours_ago=2, catalyst="earnings")
    store.upsert_items([item], NOW)
    assert store.schedule_reaction(item, "aapl", DIRECT) is True
    assert store.schedule_reaction(item, "AAPL", DIRECT) is False       # already there
    assert store.schedule_reaction(item, "MSFT", PEER) is True
    due = store.due_reactions(NOW)                                      # first try is one hour after the headline
    assert {(r.symbol, r.relation) for r in due} == {("AAPL", DIRECT), ("MSFT", PEER)}
    assert store.due_reactions(NOW - timedelta(hours=1, minutes=30)) == []

    r = next(x for x in due if x.symbol == "AAPL")
    r.p0, r.p0_at, r.p1h, r.p1h_at = 100.0, NOW - timedelta(hours=2), 101.0, NOW - timedelta(hours=1)
    r.status, r.note, r.next_try_at = "pending", "waiting for p1d, p5d", NOW + timedelta(minutes=30)
    store.save_reaction(r)
    assert store.due_reactions(NOW) == [next(x for x in store.due_reactions(NOW))] and store.due_reactions(NOW)[0].symbol == "MSFT"
    back = store.reactions_for(["x:1"], "AAPL")["x:1"]
    assert back.p0 == 100.0 and back.p1h == 101.0 and back.note == "waiting for p1d, p5d" and back.next_try_at == r.next_try_at
    assert store.reaction_counts() == {"pending": 2}


def test_reaction_summary_averages_only_filled_slots(store, make_item):
    a = make_item("x:1", "Apple beats estimates", hours_ago=30, catalyst="earnings")
    b = make_item("x:2", "Apple guidance disappoints", hours_ago=20, catalyst="earnings")
    store.upsert_items([a, b], NOW)
    for item in (a, b):
        store.schedule_reaction(item, "AAPL", DIRECT)
    ra, rb = (store.reactions_for([i.id], "AAPL")[i.id] for i in (a, b))
    ra.p0, ra.p1h, ra.p1d = 100.0, 102.0, 105.0
    rb.p0, rb.p1h = 50.0, 49.0
    store.save_reaction(ra)
    store.save_reaction(rb)
    rows = store.reaction_summary("AAPL")
    assert len(rows) == 1
    row = rows[0]
    assert row["relation"] == DIRECT and row["catalyst"] == "earnings" and row["n"] == 2
    assert abs(row["avg_1h"] - 0.0) < 1e-9 and row["n_1h"] == 2          # +2% and -2% average to zero
    assert abs(row["avg_1d"] - 5.0) < 1e-9 and row["n_1d"] == 1
    assert row["avg_5d"] is None and row["n_5d"] == 0
