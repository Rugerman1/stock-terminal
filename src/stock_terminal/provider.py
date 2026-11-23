"""The data layer the screen talks to.

`refresh` returns a Board: every quote the screen needs, the FRED values for the top
strip, and the market clock. `refresh_news` returns a NewsBoard: a tagged feed per
watchlist ticker and the state of every news source. `refresh_quant` returns a
QuantBoard: the long-term score per watchlist ticker against the universe. The screen
never imports a client directly, so tests hand it a fake provider and the real one
grows new sources in later phases without touching the UI.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Protocol

from .cache import Cache
from .clients.alpaca_client import AlpacaClient, Quote
from .clients.alpaca_news import AlpacaNewsClient
from .clients.edgar_client import EdgarClient
from .clients.finnhub_client import FinnhubClient
from .clients.fred_client import FredClient
from .config import Keys
from .datapoint import DataPoint, utcnow
from .news.engine import NewsEngine
from .news.models import NewsBoard
from .news.sentiment import build_scorer
from .news.store import NewsStore
from .quant.engine import QuantBoard, QuantEngine
from .quant.store import MarketStore


@dataclass
class Board:
    quotes: dict[str, Quote] = field(default_factory=dict)
    fred: dict[str, DataPoint] = field(default_factory=dict)   # screen label -> value
    clock: DataPoint = field(default_factory=lambda: DataPoint.missing("alpaca/clock", "not fetched"))
    refreshed_at: datetime = field(default_factory=utcnow)

    @property
    def market_open(self) -> Optional[bool]:
        if self.clock.ok and isinstance(self.clock.value, dict):
            return bool(self.clock.value.get("is_open"))
        return None

    def newest_trade_age(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds since the newest trade across all quotes, or None when no quote has a trade time."""
        now = now or utcnow()
        times = [q.last_trade_at for q in self.quotes.values() if q.last_trade_at is not None]
        if not times:
            return None
        return (now - max(times)).total_seconds()


class Provider(Protocol):
    def refresh(self, symbols: list[str]) -> Board: ...

    def refresh_news(self, tickers: list[str]) -> NewsBoard: ...

    def refresh_quant(
        self,
        tickers: list[str],
        on_board: Optional[Callable[[QuantBoard], None]] = None,
        progress: Optional[Callable[[str], None]] = None,
        force: bool = False,
    ) -> QuantBoard: ...

    def set_account(self, value: float) -> None: ...


class LiveProvider:
    def __init__(self, cfg: dict[str, Any], keys: Keys, cache: Cache, logger: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg
        self.alpaca = AlpacaClient(keys, cache, cfg, logger)
        self.fred = FredClient(keys, cache, cfg, logger)
        self.alpaca_news = AlpacaNewsClient(keys, cache, cfg, logger)
        self.finnhub = FinnhubClient(keys, cache, cfg, logger)
        self.edgar = EdgarClient(keys, cache, cfg, logger)
        self.store = NewsStore(cfg["cache"]["path"], cfg["news"]["dedupe_hours"], cfg["news"]["dedupe_similarity"])
        self.scorer = build_scorer(cfg)
        self.news = NewsEngine(cfg, self.store, self.scorer, self.alpaca, self.alpaca_news, self.finnhub, self.edgar, cache, logger)
        self.market = MarketStore(cfg["cache"]["path"])
        self.quant = QuantEngine(cfg, self.market, self.store, self.news, self.alpaca, self.finnhub, cache, logger, fred=self.fred)

    def refresh(self, symbols: list[str]) -> Board:
        clock = self.alpaca.clock()
        market_open = bool(clock.value.get("is_open")) if clock.ok and isinstance(clock.value, dict) else None
        quotes = self.alpaca.snapshots(symbols, market_open=market_open)
        fred = {label: self.fred.latest(series) for label, series in self.cfg["top_strip"]["fred"].items()}
        return Board(quotes=quotes, fred=fred, clock=clock)

    def refresh_news(self, tickers: list[str]) -> NewsBoard:
        return self.news.refresh(tickers)

    def refresh_quant(
        self,
        tickers: list[str],
        on_board: Optional[Callable[[QuantBoard], None]] = None,
        progress: Optional[Callable[[str], None]] = None,
        force: bool = False,
    ) -> QuantBoard:
        return self.quant.refresh(tickers, on_board=on_board, progress=progress, force=force)

    def peers(self, ticker: str) -> DataPoint:
        return self.finnhub.peers(ticker)

    def set_account(self, value: float) -> None:
        self.quant.set_account(value)
