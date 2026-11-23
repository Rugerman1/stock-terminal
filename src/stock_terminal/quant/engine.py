"""The long-term score, end to end: universe data, factors, z-scores, labels, signal log.

One `refresh`:
1. Read universe.yaml. Symbols = universe + watchlist + SPY (SPY for the validity engine later).
2. Daily closes: a year of split-adjusted closes per symbol, pulled once and topped up at
   most once a day (the "nightly" refresh), stored for good in data/terminal.db.
3. Fundamentals: Finnhub basic financials per symbol, cached a day.
4. Score everything: raw factors from the stored closes, fundamentals and DIRECT headline
   sentiment; z-scores against the universe; composite; labels by the universe's spread.
   The first board goes out here so the screen has labels within seconds of the data.
5. If universe news is on and a day old: pull Finnhub company news for every universe
   name so the news factor has a universe to compare with, then score again.
6. Record a signal for each watchlist ticker whose label changed (design call 1).
Anything missing stays missing and is named in the score's notes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ..cache import Cache
from ..clients.alpaca_client import AlpacaClient
from ..clients.finnhub_client import FinnhubClient
from ..datapoint import DataPoint, utcnow
from ..news.engine import NewsEngine
from ..news.models import KIND_NEWS
from ..news.store import NewsStore
from .factors import low_risk, momentum, news_sentiment, quality_components, trend, value
from .scoring import RawFactors, Score, score_all
from .store import MarketStore

Progress = Optional[Callable[[str], None]]


@dataclass
class QuantBoard:
    scores: dict[str, Score] = field(default_factory=dict)   # watchlist tickers only
    universe_name: str = ""
    universe_n: int = 0
    scored_n: int = 0
    as_of: Optional[str] = None
    run_at: Optional[datetime] = None
    status: str = ""                                         # one line for the Score tab
    notes: list[str] = field(default_factory=list)           # data problems at the universe level
    signals: list[str] = field(default_factory=list)         # label changes recorded this run
    complete: bool = True                                    # False while the universe-news pass is still to come


def load_universe(path: str | Path) -> tuple[str, list[str]]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"universe file {p} not found")
    with p.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    tickers = [str(t).strip().upper() for t in (data.get("tickers") or []) if str(t).strip()]
    if len(tickers) < 5:
        raise ValueError(f"{p} lists {len(tickers)} tickers; the score needs at least 5")
    return str(data.get("name") or p.stem), list(dict.fromkeys(tickers))


class QuantEngine:
    def __init__(
        self,
        cfg: dict[str, Any],
        store: MarketStore,
        news_store: NewsStore,
        news_engine: Optional[NewsEngine],
        alpaca: AlpacaClient,
        finnhub: FinnhubClient,
        cache: Cache,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.cfg = cfg
        self.q = cfg["quant"]
        self.store = store
        self.news_store = news_store
        self.news_engine = news_engine
        self.alpaca = alpaca
        self.finnhub = finnhub
        self.cache = cache
        self.log = logger or logging.getLogger("stock_terminal.quant")

    # ----- data refresh -----
    def _marker_fresh(self, key: str) -> bool:
        entry = self.cache.get(key)
        return entry is not None and entry.is_fresh(self.cache.now())

    def _mark(self, key: str) -> None:
        self.cache.set(key, {"at": utcnow().isoformat()}, float(self.q["universe_ttl_seconds"]))

    def refresh_bars(self, symbols: list[str], now: datetime, progress: Progress = None, force: bool = False) -> list[str]:
        """Top up daily closes for the symbols. Returns the symbols whose pull failed."""
        if not force and self._marker_fresh("quant:bars_checked"):
            return []
        history_start = now - timedelta(days=int(self.q["history_days"]))
        fresh: list[str] = []
        stale: list[str] = []
        oldest_last: Optional[str] = None
        for symbol in symbols:
            last = self.store.last_bar_date(symbol)
            if last is None:
                fresh.append(symbol)
            elif force or last < (now - timedelta(days=1)).date().isoformat():   # force: top up everything
                stale.append(symbol)
                oldest_last = last if oldest_last is None else min(oldest_last, last)
        failed: list[str] = []
        if fresh:
            if progress:
                progress(f"daily closes for {len(fresh)} new symbols")
            failed += self._store_closes(self.alpaca.daily_closes(fresh, history_start, now), fresh)
        if stale and oldest_last:
            if progress:
                progress(f"topping up daily closes for {len(stale)} symbols")
            start = datetime.fromisoformat(oldest_last).replace(tzinfo=now.tzinfo) - timedelta(days=3)
            failed += self._store_closes(self.alpaca.daily_closes(stale, start, now), stale)
        if not failed:
            self._mark("quant:bars_checked")
        return failed

    def _store_closes(self, dp: DataPoint, symbols: list[str]) -> list[str]:
        if not dp.ok:
            self.log.warning("daily closes unavailable: %s", dp.note)
            return list(symbols)
        failed = []
        for symbol in symbols:
            rows = dp.value.get(symbol) or []
            if not rows:
                failed.append(symbol)
                continue
            self.store.upsert_bars(symbol, [(r["t"], r["c"]) for r in rows])
        return failed

    def refresh_metrics(self, symbols: list[str], progress: Progress = None) -> dict[str, DataPoint]:
        out: dict[str, DataPoint] = {}
        for i, symbol in enumerate(symbols, 1):
            out[symbol] = self.finnhub.metrics(symbol)
            if progress and (i % 10 == 0 or i == len(symbols)) and out[symbol].note != "cache":
                progress(f"fundamentals {i}/{len(symbols)}")
        return out

    # ----- factors -----
    def _direct_sentiments(self, symbols: list[str], tickers: list[str], now: datetime) -> dict[str, list[tuple[float, float]]]:
        """(age in days, sentiment) per symbol for scored, non-duplicate DIRECT headlines in the news window."""
        since = now - timedelta(days=float(self.q["news_window_days"]))
        rmap = self.news_engine.load_map() if self.news_engine else None
        buckets: dict[str, list[tuple[float, float]]] = {s: [] for s in symbols}
        ticker_set = set(tickers)
        for item in self.news_store.recent(since):
            if item.is_duplicate or item.sentiment is None or item.kind != KIND_NEWS:
                continue
            age = (now - item.published_at).total_seconds() / 86400.0
            hit = {s for s in item.symbols if s in buckets}
            if rmap is not None:
                hit |= {t for t in ticker_set if t not in hit and rmap.mentions(item.headline, t)}
            for symbol in hit:
                buckets[symbol].append((age, float(item.sentiment)))
        return buckets

    def raw_factors(self, symbols: list[str], tickers: list[str], metrics: dict[str, DataPoint], now: datetime) -> dict[str, RawFactors]:
        closes_by_symbol = self.store.closes_many(symbols, limit=int(self.q["history_days"]))
        sentiments = self._direct_sentiments(symbols, tickers, now)
        raws: dict[str, RawFactors] = {}
        for symbol in symbols:
            closes = closes_by_symbol.get(symbol, [])
            price = closes[-1][1] if closes else None
            as_of = closes[-1][0] if closes else None
            dp = metrics.get(symbol)
            metric_values = dp.value if dp is not None and dp.ok else None
            bars_source = f"alpaca/iex daily closes, split-adjusted, {len(closes)} days to {as_of}" if closes else "no daily closes stored"
            if dp is not None and dp.ok:
                when = dp.fetched_at.strftime("%b %d %H:%M UTC") if dp.fetched_at else "?"
                metrics_source = f"finnhub/metric (basic financials) fetched {when}, {dp.status}"
            else:
                metrics_source = f"finnhub/metric: {dp.note if dp is not None else 'not fetched'}"
            raws[symbol] = RawFactors(
                symbol=symbol,
                as_of=as_of,
                values={
                    "momentum": momentum(closes),
                    "trend": trend(closes),
                    "low_risk": low_risk(closes),
                    "value": value(metric_values, price),
                    "news": news_sentiment(sentiments.get(symbol, []), float(self.q["news_halflife_days"]), int(self.q["news_min_headlines"])),
                },
                quality=quality_components(metric_values),
                sources={
                    "momentum": bars_source, "trend": bars_source, "low_risk": bars_source,
                    "value": metrics_source, "quality": metrics_source,
                    "news": "DIRECT headlines in the news store (Alpaca and Finnhub), FinBERT sentiment",
                },
                price=price,
            )
        return raws

    # ----- the refresh -----
    def refresh(
        self,
        tickers: list[str],
        now: Optional[datetime] = None,
        on_board: Optional[Callable[[QuantBoard], None]] = None,
        progress: Progress = None,
        force: bool = False,
    ) -> QuantBoard:
        now = now or utcnow()
        tickers = [t.upper() for t in tickers]
        try:
            universe_name, members = load_universe(self.q["universe_path"])
        except Exception as exc:  # noqa: BLE001  a bad universe file is a status line, not a crash
            self.log.warning("universe unavailable: %s", exc)
            board = QuantBoard(status=f"no score: {exc}", run_at=now, notes=[str(exc)])
            if on_board:
                on_board(board)
            return board
        symbols = list(dict.fromkeys(members + tickers + ["SPY"]))
        scorable = [s for s in symbols if s != "SPY" or "SPY" in tickers or "SPY" in members]

        notes: list[str] = []
        failed_bars = self.refresh_bars(symbols, now, progress, force)
        if failed_bars:
            notes.append(f"daily closes missing for {len(failed_bars)}: {', '.join(failed_bars[:8])}{'...' if len(failed_bars) > 8 else ''}")
        metrics = self.refresh_metrics(scorable, progress)
        missing_metrics = [s for s, dp in metrics.items() if not dp.ok]
        if missing_metrics:
            reason = metrics[missing_metrics[0]].note
            notes.append(f"fundamentals missing for {len(missing_metrics)} ({reason}): {', '.join(missing_metrics[:8])}{'...' if len(missing_metrics) > 8 else ''}")

        wants_universe_news = bool(self.q["universe_news"]) and self.news_engine is not None and self.finnhub.enabled
        news_due = wants_universe_news and (force or not self._marker_fresh("quant:universe_news"))

        board = self._score(tickers, scorable, members, universe_name, metrics, now, notes, complete=not news_due)
        if on_board:
            on_board(board)
        if not news_due:
            return board

        if progress:
            progress("pulling universe news for the news factor")
        others = [m for m in members if m not in tickers]
        result = self.news_engine.ingest_company_news(others, now - timedelta(days=float(self.q["news_window_days"])), now, progress)
        self.log.info("universe news: %s", result)
        if result["failed"]:
            notes.append(f"universe news failed for {len(result['failed'])}: {result['failed'][0]}")
        else:
            self._mark("quant:universe_news")
        board = self._score(tickers, scorable, members, universe_name, metrics, now, notes, complete=True)
        if on_board:
            on_board(board)
        return board

    def _score(
        self, tickers: list[str], scorable: list[str], members: list[str], universe_name: str,
        metrics: dict[str, DataPoint], now: datetime, notes: list[str], complete: bool,
    ) -> QuantBoard:
        raws = self.raw_factors(scorable, tickers, metrics, now)
        scores = score_all(
            raws, members, self.q["weights"], float(self.q["zscore_cap"]), float(self.q["label_top_pct"]), float(self.q["label_bottom_pct"])
        )
        self.store.save_scores(now, [s.to_row() for s in scores.values()])
        events: list[str] = []
        for ticker in tickers:
            score = scores.get(ticker)
            if score is None or score.label is None:
                continue
            previous = self.store.open_signal(ticker)
            signal = self.store.record_label(ticker, score.label, score.price, now, int(self.q["signal_horizon_days"]))
            if signal is not None:
                before = previous.label if previous else "none"
                events.append(f"{ticker}: {before} -> {score.label}")
                self.log.info("signal: %s label %s -> %s at %s", ticker, before, score.label, score.price)
        any_score = next(iter(scores.values()), None)
        scored_n = any_score.scored_n if any_score else 0
        as_of = max((s.as_of for s in scores.values() if s.as_of), default=None)
        if scored_n == 0:
            status = "no score: nothing in the universe could be scored" + (f" ({notes[0]})" if notes else "")
        else:
            status = f"scored {scored_n} of {len(members)} {universe_name} names, closes to {as_of}"
            if not complete:
                status += "; news factor fills after the universe news pull"
        return QuantBoard(
            scores={t: scores[t] for t in tickers if t in scores},
            universe_name=universe_name, universe_n=len(members), scored_n=scored_n, as_of=as_of, run_at=now,
            status=status, notes=list(notes), signals=events, complete=complete,
        )
