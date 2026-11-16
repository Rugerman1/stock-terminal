"""The data layer the screen talks to.

One `refresh` call returns a Board: every quote the screen needs, the FRED values for
the top strip, and the market clock. The screen never imports a client directly, so
tests can hand it a fake provider and the real one can grow new sources in later
phases without touching the UI.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol

from .cache import Cache
from .clients.alpaca_client import AlpacaClient, Quote
from .clients.fred_client import FredClient
from .config import Keys
from .datapoint import DataPoint, utcnow


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


class LiveProvider:
    def __init__(self, cfg: dict[str, Any], keys: Keys, cache: Cache, logger: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg
        self.alpaca = AlpacaClient(keys, cache, cfg, logger)
        self.fred = FredClient(keys, cache, cfg, logger)

    def refresh(self, symbols: list[str]) -> Board:
        clock = self.alpaca.clock()
        market_open = bool(clock.value.get("is_open")) if clock.ok and isinstance(clock.value, dict) else None
        quotes = self.alpaca.snapshots(symbols, market_open=market_open)
        fred = {label: self.fred.latest(series) for label, series in self.cfg["top_strip"]["fred"].items()}
        return Board(quotes=quotes, fred=fred, clock=clock)
