from datetime import timedelta

from textual.widgets import DataTable

from stock_terminal.app import StockTerminalApp, fmt_time_ny
from stock_terminal.clients.alpaca_client import Quote
from stock_terminal.datapoint import FRESH, MISSING, DataPoint, utcnow
from stock_terminal.provider import Board
from stock_terminal.watchlist import Watchlist


class FakeProvider:
    def __init__(self, missing=()):
        self.calls = []
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
        assert first[6] == "UNPROVEN 0/30" and first[7] == "alpaca/iex"
        assert provider.calls == [["SPY", "QQQ", "AAPL", "MSFT"]]
        assert "SPY 100.00" in app.strip_text and "VIX 18.70" in app.strip_text and "10Y 5.10%" in app.strip_text
        assert "Market CLOSED" in app.strip_text and "Newest IEX trade" in app.strip_text


async def test_missing_data_is_shown_not_invented(tmp_path, cfg):
    app, _ = make_app(tmp_path, cfg, FakeProvider(missing={"AAPL", "SPY", "QQQ", "MSFT"}))
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(app, pilot)
        table = app.query_one("#watchlist", DataTable)
        row = [str(c) for c in table.get_row_at(0)]
        assert row[1] == "--" and row[2] == "--" and row[7] == "MISSING: no Alpaca key in .env"
        assert "SPY -- (--)" in app.strip_text and "Missing: no Alpaca key in .env" in app.strip_text


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
        await pilot.press("d")  # cursor sits on the first row, AAPL
        await pilot.pause()
        assert wl.tickers == ["TSLA"]
        assert app.query_one("#watchlist", DataTable).row_count == 1


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
        assert app.board is not None


def test_fmt_time_ny_strips_leading_zero():
    from datetime import datetime, timezone
    assert fmt_time_ny(datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)) == "Mon Nov 16 9:30 AM ET"
    assert fmt_time_ny(None) == "?"
