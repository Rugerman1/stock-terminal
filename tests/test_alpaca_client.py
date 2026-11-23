from datetime import timedelta
from types import SimpleNamespace

from stock_terminal.clients.alpaca_client import NO_KEY, AlpacaClient, quotes_from_payload
from stock_terminal.config import Keys
from stock_terminal.datapoint import DELAYED, FRESH, MISSING, STALE, utcnow

NOW = utcnow()


def payload(age_seconds=10, prev_close=100.0):
    return {"AAPL": {"price": 101.0, "trade_at": (NOW - timedelta(seconds=age_seconds)).isoformat(), "prev_close": prev_close}}


def convert(p, market_open=True, status=FRESH, note="", symbols=("AAPL", "MSFT")):
    return quotes_from_payload(p, list(symbols), "alpaca/iex", status, note, NOW, market_open, 900, now=NOW)


def test_price_and_change():
    quotes = convert(payload())
    aapl = quotes["AAPL"]
    assert aapl.price.value == 101.0 and aapl.price.status == FRESH and aapl.price.source == "alpaca/iex"
    assert abs(aapl.change_pct.value - 1.0) < 1e-9 and aapl.prev_close == 100.0
    assert aapl.last_trade_at is not None


def test_symbol_absent_from_payload_is_missing():
    quotes = convert(payload())
    assert quotes["MSFT"].price.status == MISSING and quotes["MSFT"].change_pct.status == MISSING


def test_delayed_only_while_market_open():
    old = payload(age_seconds=2_000)
    assert convert(old, market_open=True)["AAPL"].price.status == DELAYED
    assert convert(old, market_open=True)["AAPL"].change_pct.status == DELAYED
    assert convert(old, market_open=False)["AAPL"].price.status == FRESH
    assert convert(old, market_open=None)["AAPL"].price.status == FRESH


def test_no_previous_close_leaves_change_missing():
    quotes = convert(payload(prev_close=None))
    assert quotes["AAPL"].price.ok and quotes["AAPL"].change_pct.status == MISSING


def test_stale_status_passes_through():
    quotes = convert(payload(), status=STALE, note="timeout; last good value 3m ago")
    assert quotes["AAPL"].price.status == STALE and "timeout" in quotes["AAPL"].price.note


def test_empty_payload_uses_the_note():
    quotes = convert(None, status=MISSING, note="no route")
    assert quotes["AAPL"].price.note == "no route"


def test_snapshots_without_key_never_touch_network(cfg, cache):
    client = AlpacaClient(Keys(), cache, cfg)
    quotes = client.snapshots(["aapl"])
    assert quotes["AAPL"].price.status == MISSING and quotes["AAPL"].price.note == NO_KEY
    assert client.clock().status == MISSING


class FakeData:
    def __init__(self):
        self.calls = 0

    def get_stock_snapshot(self, request):
        self.calls += 1
        assert sorted(request.symbol_or_symbols) == ["AAPL", "QQQ"]
        assert request.feed.value == "iex"
        trade = SimpleNamespace(price=101.0, timestamp=NOW)
        daily = SimpleNamespace(close=101.0, timestamp=NOW)
        prev = SimpleNamespace(close=100.0, timestamp=NOW - timedelta(days=1))
        return {"AAPL": SimpleNamespace(latest_trade=trade, daily_bar=daily, previous_daily_bar=prev)}


class FakeTrading:
    def get_clock(self):
        return SimpleNamespace(is_open=False, timestamp=NOW, next_open=NOW + timedelta(hours=12), next_close=NOW + timedelta(hours=19))


class FakeBars:
    def __init__(self):
        self.requests = []

    def get_stock_bars(self, request):
        self.requests.append(request)
        rows = {
            s: [SimpleNamespace(timestamp=NOW - timedelta(days=2), close=100.0 + i), SimpleNamespace(timestamp=NOW - timedelta(days=1), close=101.0 + i)]
            for i, s in enumerate(request.symbol_or_symbols)
            if s != "NOPE"
        }
        return SimpleNamespace(data=rows)


def test_daily_closes_for_many_symbols(cfg, cache):
    bars = FakeBars()
    client = AlpacaClient(Keys(), cache, cfg, data_client=bars, trading_client=FakeTrading())
    dp = client.daily_closes(["msft", "AAPL", "NOPE"], NOW - timedelta(days=10), NOW)
    assert dp.ok and dp.source == "alpaca/iex/daily"
    assert dp.value["AAPL"] == [{"t": (NOW - timedelta(days=2)).date().isoformat(), "c": 100.0}, {"t": (NOW - timedelta(days=1)).date().isoformat(), "c": 101.0}]
    assert dp.value["NOPE"] == []
    request = bars.requests[0]
    assert request.symbol_or_symbols == ["AAPL", "MSFT", "NOPE"] and request.adjustment.value == "split" and str(request.timeframe) == "1Day"
    client.daily_closes(["AAPL", "MSFT", "NOPE"], NOW - timedelta(days=10), NOW)
    assert len(bars.requests) == 1                                    # cached
    assert AlpacaClient(Keys(), cache, cfg).daily_closes(["AAPL"], NOW - timedelta(days=10), NOW).status == MISSING


def test_snapshots_with_fake_client_and_cache(cfg, cache):
    data = FakeData()
    client = AlpacaClient(Keys(), cache, cfg, data_client=data, trading_client=FakeTrading())
    quotes = client.snapshots(["qqq", "AAPL"])
    assert quotes["AAPL"].price.value == 101.0 and abs(quotes["AAPL"].change_pct.value - 1.0) < 1e-9
    assert quotes["QQQ"].price.status == MISSING
    client.snapshots(["AAPL", "QQQ"])  # same set, inside the TTL: served from cache
    assert data.calls == 1
    clock = client.clock()
    assert clock.ok and clock.value["is_open"] is False and clock.source == "alpaca/clock"
