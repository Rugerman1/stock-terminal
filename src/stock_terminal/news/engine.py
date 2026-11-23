"""The news engine: one `refresh` pulls, tags, scores, de-duplicates and tracks reactions.

Order of work on every refresh:
1. Re-read relationships.yaml (edits apply without a restart).
2. Pull headlines: Alpaca for each ticker's group (ticker, peers, ETF) and general news;
   Finnhub company news per ticker and general news; EDGAR 8-K and Form 4 per ticker.
3. Give every headline a catalyst type from the keyword table (filings bring their own).
4. Store what is new; headlines that repeat a known story are kept but marked duplicate.
5. Score unscored originals with the sentiment model.
6. Schedule a price reaction for every new original, per watchlist ticker it relates to,
   then fill the reactions whose time has come from Alpaca bars.
7. Build each ticker's feed: originals only, tagged DIRECT / PEER / SECTOR / MACRO / FILING,
   newest first, each with its reaction so far.
A source that fails shows up as a status line, never as an exception.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from ..cache import Cache
from ..clients.alpaca_client import AlpacaClient
from ..clients.alpaca_news import AlpacaNewsClient
from ..clients.edgar_client import EdgarClient
from ..clients.finnhub_client import FinnhubClient
from ..datapoint import DataPoint, FRESH, utcnow
from ..relationships import Relationships
from .models import KIND_NEWS, FeedEntry, NewsBoard, NewsItem, Reaction, relation_for
from .reactions import RETRY_AFTER, bars_from_payload, daily_window, fill, minute_window, needs_daily, needs_minutes
from .sentiment import Scorer
from .store import NewsStore


def describe_status(dp: DataPoint, count: Optional[int] = None) -> str:
    if dp.status == FRESH:
        text = "ok"
        if count is not None:
            text += f", {count} items"
        if dp.note == "cache":
            text += " (cache)"
        return text
    return f"{dp.status.upper()}: {dp.note}"


class NewsEngine:
    def __init__(
        self,
        cfg: dict[str, Any],
        store: NewsStore,
        scorer: Scorer,
        alpaca: AlpacaClient,
        alpaca_news: AlpacaNewsClient,
        finnhub: FinnhubClient,
        edgar: EdgarClient,
        cache: Optional[Cache] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.scorer = scorer
        self.alpaca = alpaca
        self.alpaca_news = alpaca_news
        self.finnhub = finnhub
        self.edgar = edgar
        self.cache = cache
        self.log = logger or logging.getLogger("stock_terminal.news")
        news_cfg = cfg["news"]
        self.lookback = timedelta(days=float(news_cfg["lookback_days"]))
        self.filings_lookback = timedelta(days=float(news_cfg["filings_lookback_days"]))
        self.reactions_per_refresh = int(news_cfg["reactions_per_refresh"])
        self.relationships_path = news_cfg["relationships_path"]

    def load_map(self) -> Relationships:
        try:
            return Relationships.load(self.relationships_path)
        except Exception as exc:  # noqa: BLE001  a broken YAML must not stop the news; it shows as a note
            self.log.warning("relationships.yaml could not be read: %s", exc)
            rmap = Relationships({})
            rmap.load_error = str(exc)[:160]  # type: ignore[attr-defined]
            return rmap

    # ----- the refresh -----
    def refresh(self, tickers: list[str], now: Optional[datetime] = None) -> NewsBoard:
        now = now or utcnow()
        tickers = [t.upper() for t in tickers]
        rmap = self.load_map()
        since = now - self.lookback
        filings_since = now - self.filings_lookback
        status: dict[str, str] = {}
        collected: list[NewsItem] = []

        for ticker in tickers:
            items, dp = self.alpaca_news.news(rmap.for_ticker(ticker).symbols, since)
            status[f"alpaca news {ticker}"] = describe_status(dp, len(items))
            collected.extend(items)
        # Market-wide news comes from symbols, not from the unfiltered feed: on the first live run (2026-11-22) the
        # unfiltered 50 newest were small-cap press releases with nothing macro in them. Benzinga files Fed, tariff
        # and sector stories under SPY, QQQ, TLT and the sector ETFs.
        market = list(dict.fromkeys(list(self.cfg["news"]["market_symbols"]) + [m.sector_etf for m in (rmap.for_ticker(t) for t in tickers) if m.sector_etf]))
        items, dp = self.alpaca_news.news(market, since)
        status["alpaca market"] = describe_status(dp, len(items))
        collected.extend(items)

        for ticker in tickers:
            items, dp = self.finnhub.company_news(ticker, since, now)
            status[f"finnhub news {ticker}"] = describe_status(dp, len(items))
            collected.extend(items)
        items, dp = self.finnhub.general_news()
        status["finnhub general"] = describe_status(dp, len(items))
        collected.extend(items)

        for ticker in tickers:
            items, dp = self.edgar.filings(ticker, filings_since)
            status[f"edgar {ticker}"] = describe_status(dp, len(items))
            collected.extend(items)

        for item in collected:
            if item.kind == KIND_NEWS:
                item.catalyst = rmap.catalyst_of(item.headline)

        new_items, duplicates = self.store.upsert_items(collected, now)
        if new_items or duplicates:
            self.log.info("news: %d new, %d duplicates, %d total stored", len(new_items), duplicates, self.store.count())

        self._score_unscored(min(since, filings_since))

        for item in new_items:
            for ticker in tickers:
                relation = relation_for(item, ticker, rmap)
                if relation is not None:
                    self.store.schedule_reaction(item, ticker, relation)

        self.fill_reactions(now)

        feeds = {t: self.feed(t, rmap, since, filings_since) for t in tickers}
        map_notes = {}
        load_error = getattr(rmap, "load_error", "")
        for t in tickers:
            if load_error:
                map_notes[t] = f"relationships.yaml unreadable ({load_error}); DIRECT and FILING only"
            elif not rmap.for_ticker(t).listed:
                map_notes[t] = "not in relationships.yaml: DIRECT and FILING only (--peers prints a snippet)"
        if self.cache is not None:
            self.cache.purge_expired()
        return NewsBoard(
            feeds=feeds,
            source_status=status,
            scorer_status=self.scorer.status(),
            map_notes=map_notes,
            new_items=len(new_items),
            duplicates=duplicates,
            refreshed_at=now,
        )

    def ingest_company_news(self, symbols: list[str], since: datetime, now: Optional[datetime] = None, progress: Any = None) -> dict[str, Any]:
        """Pull Finnhub company news for symbols outside the watchlist (the score's universe), store and score it.

        Returns counts and the symbols whose pull failed. Headlines land in the same store, so a universe name that is
        also a peer shows up in the watchlist feeds."""
        now = now or utcnow()
        rmap = self.load_map()
        collected: list[NewsItem] = []
        failed: list[str] = []
        for i, symbol in enumerate(symbols, 1):
            items, dp = self.finnhub.company_news(symbol, since, now)
            if not dp.ok:
                failed.append(f"{symbol}: {dp.note}")
            collected.extend(items)
            if progress and (i % 10 == 0 or i == len(symbols)):
                progress(f"universe news {i}/{len(symbols)}")
        for item in collected:
            if item.kind == KIND_NEWS:
                item.catalyst = rmap.catalyst_of(item.headline)
        new_items, duplicates = self.store.upsert_items(collected, now)
        if progress and new_items:
            progress(f"scoring {len(new_items)} new universe headlines")
        self._score_unscored(since)
        return {"pulled": len(collected), "new": len(new_items), "duplicates": duplicates, "failed": failed}

    def _score_unscored(self, since: datetime) -> None:
        unscored = self.store.unscored(since, limit=int(self.cfg["sentiment"].get("max_per_refresh", 1000)))
        if not unscored:
            return
        scores = self.scorer.score([item.headline for item in unscored])
        scored = 0
        for item, score in zip(unscored, scores):
            if score is None:
                # Leave sentiment NULL so the next refresh tries again (the model may just not be loaded yet).
                self.store.set_sentiment(item.id, None, self.scorer.status())
            else:
                self.store.set_sentiment(item.id, float(score), self.scorer.name)
                scored += 1
        if scored:
            self.log.info("news: scored %d headlines with %s", scored, self.scorer.name)

    # ----- reactions -----
    def fill_reactions(self, now: Optional[datetime] = None) -> int:
        """Fill the reactions whose time has come. Returns how many rows were touched."""
        now = now or utcnow()
        touched = 0
        for reaction in self.store.due_reactions(now, self.reactions_per_refresh):
            self._fill_one(reaction, now)
            self.store.save_reaction(reaction)
            touched += 1
        return touched

    def _fill_one(self, reaction: Reaction, now: datetime) -> None:
        minutes = daily = None
        problem = ""
        if needs_minutes(reaction):
            start, end = minute_window(reaction)
            dp = self.alpaca.bars(reaction.symbol, "minute", start, end)
            if dp.ok:
                minutes = bars_from_payload(dp.value)
            else:
                problem = dp.note
        if needs_daily(reaction) and not problem and (reaction.p0_at is not None or minutes):
            start, end = daily_window(reaction)
            dp = self.alpaca.bars(reaction.symbol, "day", start, end)
            if dp.ok:
                daily = bars_from_payload(dp.value)
            else:
                problem = dp.note
        fill(reaction, minutes, daily, now)
        if problem and reaction.status == "pending":
            reaction.note = f"bars unavailable: {problem}"
            reaction.next_try_at = now + RETRY_AFTER

    # ----- feeds -----
    def feed(self, ticker: str, rmap: Relationships, since: datetime, filings_since: datetime) -> list[FeedEntry]:
        ticker = ticker.upper()
        entries: list[FeedEntry] = []
        for item in self.store.recent(min(since, filings_since)):
            if item.is_duplicate:
                continue
            if item.kind == KIND_NEWS and item.published_at < since:
                continue
            relation = relation_for(item, ticker, rmap)
            if relation is None:
                continue
            entries.append(FeedEntry(item=item, relation=relation))
        reactions = self.store.reactions_for([e.item.id for e in entries], ticker)
        for entry in entries:
            entry.reaction = reactions.get(entry.item.id)
            if entry.reaction is None:
                # A ticker added after the headline was stored: start tracking it from here.
                self.store.schedule_reaction(entry.item, ticker, entry.relation)
        return entries
