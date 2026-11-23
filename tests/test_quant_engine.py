from datetime import datetime, timedelta, timezone

import yaml

from stock_terminal.datapoint import FRESH, MISSING, DataPoint
from stock_terminal.news.models import NewsItem
from stock_terminal.news.store import NewsStore
from stock_terminal.quant.engine import QuantEngine, load_universe
from stock_terminal.quant.store import MarketStore
from stock_terminal.relationships import Relationships

NOW = datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)
MEMBERS = ["U0", "U1", "U2", "U3", "U4", "U5", "U6", "AAPL"]


class FakeAlpaca:
    """A year of daily closes per symbol; symbol i grows 0.05% x i a day, so momentum ranks them in order."""

    enabled = True

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def daily_closes(self, symbols, start, end):
        self.calls.append(list(symbols))
        out = {}
        for s in symbols:
            if s in self.fail:
                out[s] = []
                continue
            idx = MEMBERS.index(s) if s in MEMBERS else 3
            growth = 1.0 + 0.0005 * idx
            day = start.date()
            rows = []
            i = 0
            while day <= end.date():
                if day.weekday() < 5:
                    rows.append({"t": day.isoformat(), "c": 100.0 * growth ** i})
                    i += 1
                day += timedelta(days=1)
            out[s] = rows
        return DataPoint(out, "alpaca/iex/daily", FRESH)


class FakeFinnhub:
    enabled = True

    def __init__(self):
        self.metric_calls = []

    def metrics(self, symbol):
        self.metric_calls.append(symbol)
        idx = MEMBERS.index(symbol) if symbol in MEMBERS else 2
        if symbol == "SPY":
            return DataPoint.missing("finnhub/metric", "no fundamentals for an ETF")
        return DataPoint(
            {"epsTTM": 5.0 + idx, "pfcfShareTTM": 20.0, "roeTTM": 10.0 + idx, "grossMarginTTM": 40.0 + idx, "totalDebt/totalEquityAnnual": 0.5},
            "finnhub/metric", FRESH, fetched_at=NOW,
        )


class FakeNewsEngine:
    def __init__(self):
        self.ingested = []

    def load_map(self):
        return Relationships({"tickers": {"AAPL": {"aliases": ["Apple"]}}})

    def ingest_company_news(self, symbols, since, now=None, progress=None):
        self.ingested.append(list(symbols))
        if progress:
            progress("universe news done")
        return {"pulled": 0, "new": 0, "duplicates": 0, "failed": []}


def make(tmp_path, cfg, cache, universe_news=False, news_engine=None, alpaca=None, finnhub=None):
    path = tmp_path / "universe.yaml"
    path.write_text(yaml.safe_dump({"name": "Test 8", "tickers": MEMBERS}), encoding="utf-8")
    cfg["quant"]["universe_path"] = str(path)
    cfg["quant"]["universe_news"] = universe_news
    market = MarketStore(tmp_path / "m.db")
    news = NewsStore(tmp_path / "n.db")
    engine = QuantEngine(cfg, market, news, news_engine, alpaca or FakeAlpaca(), finnhub or FakeFinnhub(), cache)
    return engine, market, news


def test_load_universe(tmp_path):
    path = tmp_path / "u.yaml"
    path.write_text("name: Tiny\ntickers: [aapl, MSFT, msft, NVDA, GOOG, META]\n", encoding="utf-8")
    assert load_universe(path) == ("Tiny", ["AAPL", "MSFT", "NVDA", "GOOG", "META"])


def test_first_refresh_pulls_scores_and_logs_signals(tmp_path, cfg, cache):
    alpaca = FakeAlpaca()
    engine, market, news = make(tmp_path, cfg, cache, alpaca=alpaca)
    boards = []
    progress = []
    board = engine.refresh(["AAPL", "ZZZ"], now=NOW, on_board=boards.append, progress=progress.append)
    assert boards == [board] and board.complete and board.universe_name == "Test 8" and board.universe_n == 8
    assert board.scored_n == 8 and board.as_of == "2026-11-20"
    assert set(board.scores) == {"AAPL", "ZZZ"}
    aapl = board.scores["AAPL"]
    assert aapl.label == "Favorable" and aapl.percentile == 87.5          # the fastest grower of eight, by construction
    assert aapl.factor("momentum").z > 0 and aapl.factor("value").z is not None and aapl.factor("quality").z is not None
    assert "news missing" in " | ".join(aapl.notes)
    assert board.scores["ZZZ"].label is not None and not board.scores["ZZZ"].in_universe
    assert alpaca.calls == [MEMBERS + ["ZZZ", "SPY"]] and market.bar_counts()["SPY"] > 250
    assert market.latest_scores()["U0"]["label"] == "Unfavorable"
    assert board.signals == ["AAPL: none -> Favorable", f"ZZZ: none -> {board.scores['ZZZ'].label}"]
    assert any("fundamentals missing for 1 (no fundamentals for an ETF): SPY" in n for n in board.notes) is False  # SPY is not scored
    assert progress and progress[0].startswith("daily closes for 10 new symbols")

    # Second refresh the same day: bars are not pulled again, the label is unchanged, no new signal.
    board2 = engine.refresh(["AAPL", "ZZZ"], now=NOW + timedelta(hours=1))
    assert alpaca.calls == [MEMBERS + ["ZZZ", "SPY"]] and board2.signals == [] and len(market.signals("AAPL")) == 1
    # A forced refresh pulls again.
    engine.refresh(["AAPL"], now=NOW + timedelta(hours=2), force=True)
    assert len(alpaca.calls) == 2


def test_news_factor_uses_direct_headlines_and_universe_news_pass(tmp_path, cfg, cache):
    fake_news = FakeNewsEngine()
    engine, market, news = make(tmp_path, cfg, cache, universe_news=True, news_engine=fake_news)
    headlines = [
        "Apple beats on iPhone demand", "Apple opens a flagship in Mumbai", "Apple wins a patent appeal",
        "Apple raises its dividend", "Apple hires a chip veteran", "Apple expands the India supply chain",
    ]   # six different stories: the dedupe would merge "Apple story 0..5"
    items = [
        NewsItem(id=f"n:{i}", source="finnhub/x", headline=h, published_at=NOW - timedelta(days=i), symbols=["AAPL"], sentiment=0.5)
        for i, h in enumerate(headlines)
    ]
    items.append(NewsItem(id="n:alias", source="finnhub/x", headline="Apple unveils a thing", published_at=NOW - timedelta(hours=1), symbols=[], sentiment=0.9))
    news.upsert_items(items, NOW)
    for item in items:
        news.set_sentiment(item.id, item.sentiment, "fake")
    boards = []
    board = engine.refresh(["AAPL"], now=NOW, on_board=boards.append)
    assert [b.complete for b in boards] == [False, True] and board.complete
    assert fake_news.ingested == [[m for m in MEMBERS if m != "AAPL"]]
    aapl = board.scores["AAPL"]
    news_factor = aapl.factor("news")
    assert news_factor.raw is not None and news_factor.inputs["headlines"] == 7          # six tagged plus the alias match
    assert news_factor.z is None and "no universe spread (n=1)" in news_factor.note     # nobody else has news
    assert "news factor fills" not in board.status


def test_missing_universe_file_is_a_status_not_a_crash(tmp_path, cfg, cache):
    engine, _, _ = make(tmp_path, cfg, cache)
    cfg["quant"]["universe_path"] = str(tmp_path / "nope.yaml")
    board = engine.refresh(["AAPL"], now=NOW)
    assert board.status.startswith("no score") and board.scores == {}


def test_failed_bars_and_metrics_are_named(tmp_path, cfg, cache):
    engine, market, _ = make(tmp_path, cfg, cache, alpaca=FakeAlpaca(fail={"U3"}))
    board = engine.refresh(["AAPL"], now=NOW)
    assert any(n.startswith("daily closes missing for 1: U3") for n in board.notes)
    assert cache.get("quant:bars_checked") is None                                     # not marked done while something failed
    u3 = market.latest_scores()["U3"]
    assert board.scored_n == 8 and u3["composite"] is not None                         # fundamentals alone still score, with the gaps named
    assert "momentum missing" in " | ".join(u3["factors"]["notes"])
