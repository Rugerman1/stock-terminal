from datetime import datetime, timedelta, timezone

from conftest import NOW
from stock_terminal.datapoint import FRESH, DataPoint
from stock_terminal.news.engine import NewsEngine, describe_status
from stock_terminal.news.models import DIRECT, FILING, KIND_FILING, MACRO, PEER, NewsItem
from stock_terminal.news.store import NewsStore

UTC = timezone.utc


def item(item_id, headline, hours_ago, symbols=(), kind="news", catalyst="other", url=""):
    return NewsItem(id=item_id, source="fake/src", headline=headline, published_at=NOW - timedelta(hours=hours_ago), url=url, symbols=list(symbols), kind=kind, catalyst=catalyst)


class FakeAlpacaNews:
    def __init__(self):
        self.calls = []

    def news(self, symbols, since):
        self.calls.append(symbols)
        if symbols is not None and "SPY" in symbols:
            assert symbols[0] == "SPY"   # the map's sector ETFs follow when the ticker has one
            return [item("alpaca:fed", "Fed signals a rate cut in December", 5, ["SPY"])], DataPoint([], "alpaca/news", FRESH)
        if "AAPL" in symbols:
            return [
                item("alpaca:1", "Apple reports record quarterly earnings", 3, ["AAPL"], url="https://x/1"),
                item("alpaca:2", "Microsoft lifts cloud guidance", 4, ["MSFT"]),
            ], DataPoint([], "alpaca/news", FRESH)
        return [item("alpaca:2", "Microsoft lifts cloud guidance", 4, ["MSFT"])], DataPoint([], "alpaca/news", FRESH)


class FakeFinnhub:
    def company_news(self, symbol, since, until):
        if symbol == "AAPL":
            # The same wire story Alpaca already carried, from another outlet.
            return [item("finnhub:1", "Apple reports record quarterly earnings - Reuters", 2, ["AAPL"])], DataPoint([], "finnhub/company-news", FRESH)
        return [], DataPoint([], "finnhub/company-news", FRESH)

    def general_news(self):
        return [], DataPoint.missing("finnhub/general", "no Finnhub key in .env")


class FakeEdgar:
    def filings(self, ticker, since):
        if ticker == "AAPL":
            return [item("edgar:1", "8-K: results of operations (earnings)", 30, ["AAPL"], kind=KIND_FILING, catalyst="earnings")], DataPoint([], "edgar/submissions", FRESH)
        return [], DataPoint([], "edgar/submissions", FRESH)


class FakeAlpacaBars:
    """Minute and daily bars from a fixed price path, so reactions can be filled without a network."""

    def __init__(self):
        self.calls = []

    def bars(self, symbol, timeframe, start, end):
        self.calls.append((symbol, timeframe))
        rows = []
        if timeframe == "minute":
            t = start.replace(second=0, microsecond=0)
            for i in range(6 * 60):
                rows.append({"t": (t + timedelta(minutes=i)).isoformat(), "c": 100.0 + 0.01 * i})
        else:
            day = start.replace(hour=5, minute=0, second=0, microsecond=0)
            for i in range(14):
                rows.append({"t": (day + timedelta(days=i)).isoformat(), "c": 100.0 + i})
        return DataPoint(rows, "alpaca/iex/bars", FRESH)


class FakeScorer:
    name = "fake"

    def __init__(self, fail=False):
        self.fail = fail

    def score(self, texts):
        return [None if self.fail else (0.5 if "record" in t else -0.2) for t in texts]

    def status(self):
        return "MISSING: fake down" if self.fail else "ok (fake)"


def make_engine(cfg, tmp_path, scorer=None):
    store = NewsStore(tmp_path / "news.db")
    bars = FakeAlpacaBars()
    engine = NewsEngine(cfg, store, scorer or FakeScorer(), bars, FakeAlpacaNews(), FakeFinnhub(), FakeEdgar())
    return engine, store, bars


def test_refresh_builds_tagged_feeds(cfg, tmp_path):
    engine, store, bars = make_engine(cfg, tmp_path)
    board = engine.refresh(["AAPL", "MSFT"], now=NOW)
    assert board.new_items == 4 and board.duplicates == 1 and store.count() == 5
    aapl = {e.item.id: e for e in board.feeds["AAPL"]}
    assert set(aapl) == {"alpaca:1", "alpaca:2", "alpaca:fed", "edgar:1"}       # the Reuters copy is hidden as a duplicate
    assert aapl["alpaca:1"].relation == DIRECT and aapl["alpaca:2"].relation == PEER
    assert aapl["alpaca:fed"].relation == MACRO and aapl["edgar:1"].relation == FILING
    assert [e.item.id for e in board.feeds["AAPL"]][0] == "alpaca:1"              # newest first
    assert aapl["alpaca:1"].item.catalyst == "earnings" and aapl["alpaca:2"].item.catalyst == "guidance"
    assert aapl["alpaca:1"].item.sentiment == 0.5 and aapl["alpaca:fed"].item.sentiment == -0.2
    msft = {e.item.id: e.relation for e in board.feeds["MSFT"]}
    assert msft == {"alpaca:2": DIRECT, "alpaca:1": PEER, "alpaca:fed": MACRO}  # AAPL is on MSFT's peer list; the 8-K is not
    assert board.source_status["finnhub general"].startswith("MISSING: no Finnhub key")
    assert board.source_status["alpaca news AAPL"] == "ok, 2 items" and board.scorer_status == "ok (fake)"
    assert board.map_notes == {}


def test_reactions_are_scheduled_and_filled(cfg, tmp_path):
    engine, store, bars = make_engine(cfg, tmp_path)
    engine.refresh(["AAPL", "MSFT"], now=NOW)
    counts = store.reaction_counts()
    # 4 AAPL entries + 3 MSFT entries, each a (headline, ticker) row. Every one is due (all headlines are over an hour old).
    assert sum(counts.values()) == 7
    aapl = store.reactions_for(["alpaca:1"], "AAPL")["alpaca:1"]
    assert aapl.p0 == 100.0 and aapl.p1h is not None and aapl.p1d is not None
    assert bars.calls and all(sym in ("AAPL", "MSFT") for sym, _ in bars.calls)
    board = engine.refresh(["AAPL", "MSFT"], now=NOW + timedelta(days=8))
    assert board.new_items == 0
    assert store.reaction_counts().get("done", 0) >= 1


def test_scorer_failure_leaves_sentiment_missing_and_retries(cfg, tmp_path):
    scorer = FakeScorer(fail=True)
    engine, store, _ = make_engine(cfg, tmp_path, scorer)
    board = engine.refresh(["AAPL"], now=NOW)
    entry = next(e for e in board.feeds["AAPL"] if e.item.id == "alpaca:1")
    assert entry.item.sentiment is None and entry.item.sentiment_note == "MISSING: fake down"
    scorer.fail = False
    board = engine.refresh(["AAPL"], now=NOW + timedelta(minutes=10))
    entry = next(e for e in board.feeds["AAPL"] if e.item.id == "alpaca:1")
    assert entry.item.sentiment == 0.5


def test_unlisted_ticker_gets_a_map_note(cfg, tmp_path):
    engine, store, _ = make_engine(cfg, tmp_path)
    board = engine.refresh(["TSLA"], now=NOW)
    assert "not in relationships.yaml" in board.map_notes["TSLA"]
    assert board.feeds["TSLA"] == []


def test_describe_status():
    assert describe_status(DataPoint([], "s", FRESH), 3) == "ok, 3 items"
    assert describe_status(DataPoint([], "s", FRESH, note="cache"), 0) == "ok, 0 items (cache)"
    assert describe_status(DataPoint.missing("s", "boom")) == "MISSING: boom"
