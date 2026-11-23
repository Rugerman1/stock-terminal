from datetime import timedelta

from textual.widgets import DataTable, ListView, Static

from stock_terminal.app import StockTerminalApp, entry_markup, fmt_time_ny, short_source
from stock_terminal.clients.alpaca_client import Quote
from stock_terminal.datapoint import FRESH, MISSING, DataPoint, utcnow
from stock_terminal.news.models import DIRECT, MACRO, FeedEntry, NewsBoard, NewsItem, Reaction
from stock_terminal.provider import Board
from stock_terminal.watchlist import Watchlist


def news_item(item_id, headline, hours_ago, symbols=(), sentiment=None, url=""):
    return NewsItem(id=item_id, source="alpaca/benzinga", headline=headline, published_at=utcnow() - timedelta(hours=hours_ago), url=url, symbols=list(symbols), sentiment=sentiment, catalyst="earnings")


class FakeProvider:
    def __init__(self, missing=()):
        self.calls = []
        self.news_calls = []
        self.missing = set(missing)

    def refresh(self, symbols):
        self.calls.append(list(symbols))
        now = utcnow()
        quotes = {}
        for i, s in enumerate(symbols):
            if s in self.missing:
                quotes[s] = Quote.missing(s, "alpaca/iex", "no Alpaca key in .env")
            else:
                quotes[s] = Quote(
                    s,
                    DataPoint(100.0 + i, "alpaca/iex", FRESH, as_of=now - timedelta(seconds=5)),
                    DataPoint(0.5 * i, "alpaca/iex", FRESH, as_of=now),
                    last_trade_at=now - timedelta(seconds=5),
                    prev_close=100.0,
                )
        fred = {
            "VIX": DataPoint(18.7, "fred/VIXCLS", FRESH, as_of=now - timedelta(days=1)),
            "10Y": DataPoint(5.1, "fred/DGS10", FRESH, as_of=now - timedelta(days=1)),
        }
        clock = DataPoint(
            {"is_open": False, "next_open": (now + timedelta(hours=12)).isoformat(), "next_close": (now + timedelta(hours=19)).isoformat()},
            "alpaca/clock",
            FRESH,
        )
        return Board(quotes=quotes, fred=fred, clock=clock)

    def refresh_news(self, tickers):
        self.news_calls.append(list(tickers))
        feeds = {t: [] for t in tickers}
        if "AAPL" in feeds:
            direct = news_item("alpaca:1", "Apple reports record quarterly earnings", 2, ["AAPL"], sentiment=0.62, url="https://example.com/1")
            reaction = Reaction("alpaca:1", "AAPL", DIRECT, "earnings", direct.published_at, p0=100.0, p1h=100.5)
            feeds["AAPL"] = [
                FeedEntry(direct, DIRECT, reaction),
                FeedEntry(news_item("alpaca:2", "Fed signals a rate cut", 5), MACRO, None),
            ]
        return NewsBoard(
            feeds=feeds,
            source_status={"alpaca news AAPL": "ok, 2 items", "finnhub general": "MISSING: no Finnhub key in .env"},
            scorer_status="ok (fake)",
            map_notes={t: "not in relationships.yaml: DIRECT and FILING only" for t in tickers if t == "TSLA"},
            new_items=2,
            duplicates=0,
            refreshed_at=utcnow(),
        )


def make_app(tmp_path, cfg, provider=None, tickers=("AAPL", "MSFT")):
    wl = Watchlist(tmp_path / "w.yaml")
    for t in tickers:
        wl.add(t)
    return StockTerminalApp(cfg, wl, provider or FakeProvider(), periodic_refresh=False), wl


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_table_and_strip_render(tmp_path, cfg):
    provider = FakeProvider()
    app, _ = make_app(tmp_path, cfg, provider)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(app, pilot)
        table = app.query_one("#watchlist", DataTable)
        assert table.row_count == 2
        first = [str(c) for c in table.get_row_at(0)]
        assert first[0] == "AAPL" and first[1] == "102.00" and first[2] == "+1.00%"
        assert first[6] == "UNPROVEN 0/30" and first[7] == "ok"
        assert provider.calls == [["SPY", "QQQ", "AAPL", "MSFT"]]
        assert "SPY 100.00" in app.strip_text and "VIX 18.70" in app.strip_text and "10Y 5.10%" in app.strip_text
        assert "Market CLOSED" in app.strip_text and "Newest IEX trade" in app.strip_text


async def test_missing_data_is_shown_not_invented(tmp_path, cfg):
    app, _ = make_app(tmp_path, cfg, FakeProvider(missing={"AAPL", "SPY", "QQQ", "MSFT"}))
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(app, pilot)
        table = app.query_one("#watchlist", DataTable)
        row = [str(c) for c in table.get_row_at(0)]
        assert row[1] == "--" and row[2] == "--" and row[7] == "MISSING"
        assert "SPY -- (--)" in app.strip_text and "Missing: no Alpaca key in .env" in app.strip_text


async def test_news_pane_follows_the_cursor_and_filter(tmp_path, cfg):
    provider = FakeProvider()
    app, _ = make_app(tmp_path, cfg, provider)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(app, pilot)
        assert provider.news_calls == [["AAPL", "MSFT"]]
        listing = app.query_one("#news-list", ListView)
        assert len(listing.children) == 2 and len(app.shown_entries) == 2
        head = app.news_head_text
        assert head.startswith("AAPL news  filter ALL  2 of 2") and "sentiment ok (fake)" in head
        assert "sources: finnhub MISSING: no Finnhub key in .env" in head and "alpaca" not in head.split("sources:")[1]
        first = entry_markup(app.shown_entries[0])
        assert "DIRECT" in first and "earnings" in first and "+0.62" in first and "+1h +0.5%" in first

        await pilot.press("f")                                  # ALL -> DIRECT
        await pilot.pause()
        assert app.news_filter == "DIRECT" and len(app.shown_entries) == 1
        for _ in range(5):                                      # back round to ALL
            await pilot.press("f")
        await pilot.pause()
        assert app.news_filter == "ALL"

        await pilot.press("down")                               # cursor to MSFT, which has no headlines
        await pilot.pause()
        assert app.news_head_text.startswith("MSFT news  filter ALL  0 of 0")
        assert len(app.shown_entries) == 0 and len(listing.children) == 1   # the "no headlines" line

        await pilot.press("o")                                  # nothing highlighted in the list: a warning, no crash
        await pilot.pause()


async def test_add_and_delete_keys(tmp_path, cfg):
    provider = FakeProvider()
    app, wl = make_app(tmp_path, cfg, provider, tickers=("AAPL",))
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(app, pilot)
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("t", "s", "l", "a", "enter")
        await settle(app, pilot)
        assert wl.tickers == ["AAPL", "TSLA"]
        assert "- TSLA" in (tmp_path / "w.yaml").read_text(encoding="utf-8")
        assert app.query_one("#watchlist", DataTable).row_count == 2
        assert provider.calls[-1] == ["SPY", "QQQ", "AAPL", "TSLA"]  # a refresh followed the add
        assert provider.news_calls[-1] == ["AAPL", "TSLA"]
        await pilot.press("d")  # cursor sits on the first row, AAPL
        await pilot.pause()
        assert wl.tickers == ["TSLA"]
        assert app.query_one("#watchlist", DataTable).row_count == 1
        assert "not in relationships.yaml" in app.news_head_text


async def test_invalid_ticker_is_refused(tmp_path, cfg):
    app, wl = make_app(tmp_path, cfg, tickers=("AAPL",))
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(app, pilot)
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("dollar_sign", "enter")
        await settle(app, pilot)
        assert wl.tickers == ["AAPL"]
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert wl.tickers == ["AAPL"]


async def test_explain_refresh_and_log_keys_do_not_crash(tmp_path, cfg):
    app, _ = make_app(tmp_path, cfg)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(app, pilot)
        await pilot.press("question_mark", "r", "l")
        await settle(app, pilot)
        assert app.board is not None and app.news_board is not None


def test_fmt_time_ny_strips_leading_zero():
    from datetime import datetime, timezone
    assert fmt_time_ny(datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)) == "Mon Nov 16 9:30 AM ET"
    assert fmt_time_ny(None) == "?"


def test_entry_markup_escapes_and_shows_missing_sentiment():
    item = news_item("x:1", "Apple [beats] estimates", 3)
    text = entry_markup(FeedEntry(item, DIRECT, None))
    assert "sent --" in text and "Apple \\[beats] estimates" in text and "reaction: not tracked" in text
    assert short_source("edgar/Form 4") == "Form 4" and short_source("plain") == "plain"
