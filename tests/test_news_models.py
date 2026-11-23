from datetime import datetime, timezone

from stock_terminal.news.models import DIRECT, FILING, KIND_FILING, MACRO, PEER, SECTOR, Reaction, parse_when, relation_for


def test_parse_when_accepts_the_shapes_the_sources_use():
    z = parse_when("2026-11-20T16:05:12.000Z")
    assert z == datetime(2026, 11, 20, 16, 5, 12, tzinfo=timezone.utc)
    assert parse_when(1_795_190_400) == datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)
    assert parse_when(datetime(2026, 11, 20, 12, 0)).tzinfo is not None
    assert parse_when("2026-11-20").hour == 0
    assert parse_when("not a date") is None and parse_when(None) is None and parse_when("") is None


def test_relation_precedence(rmap, make_item):
    msft_story = make_item("a:1", "Microsoft lifts cloud guidance", symbols=["MSFT"])
    assert relation_for(msft_story, "MSFT", rmap) == DIRECT
    assert relation_for(msft_story, "AAPL", rmap) == PEER          # MSFT is on AAPL's peer list
    assert relation_for(msft_story, "NVDA", rmap) is None          # not a peer, no sector or topic words

    by_name = make_item("a:2", "Apple to unveil new iPhone next week")
    assert relation_for(by_name, "AAPL", rmap) == DIRECT           # alias match, no symbol attached

    etf = make_item("a:3", "Tech ETF inflows surge", symbols=["XLK"])
    assert relation_for(etf, "AAPL", rmap) == SECTOR and relation_for(etf, "NVDA", rmap) is None

    sector_words = make_item("a:4", "Chip stocks rally on export control relief")
    assert relation_for(sector_words, "NVDA", rmap) == SECTOR      # SMH keywords, before the chips_export topic

    macro = make_item("a:5", "Fed signals a rate cut in December")
    assert relation_for(macro, "AAPL", rmap) == MACRO
    assert relation_for(macro, "TSLA", rmap) is None               # unlisted ticker has no topics

    filing = make_item("e:1", "8-K: results of operations (earnings)", symbols=["AAPL"], kind=KIND_FILING)
    assert relation_for(filing, "AAPL", rmap) == FILING
    assert relation_for(filing, "MSFT", rmap) is None              # filings are never PEER news


def test_reaction_moves():
    r = Reaction("n", "AAPL", DIRECT, "earnings", datetime(2026, 11, 20, tzinfo=timezone.utc), p0=100.0, p1h=101.0, p1d=None)
    assert abs(r.move("p1h") - 1.0) < 1e-9 and r.move("p1d") is None and not r.is_complete()
    r.p1d, r.p5d = 99.0, 110.0
    assert r.is_complete() and abs(r.move("p5d") - 10.0) < 1e-9


def test_item_row_round_trip(make_item):
    item = make_item("a:9", "  Spaced   headline ", symbols=["aapl", "AAPL", "msft"], url="https://x/y")
    assert item.headline == "Spaced headline" and item.symbols == ["AAPL", "MSFT"]
    row = item.to_row()
    from stock_terminal.news.models import NewsItem
    back = NewsItem.from_row(row)
    assert back.id == item.id and back.published_at == item.published_at and back.symbols == ["AAPL", "MSFT"]
