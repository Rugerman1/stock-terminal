"""The shapes the news engine passes around.

A NewsItem is one headline or filing, stored once whatever source it came from. The
relation tag (DIRECT, PEER, SECTOR, MACRO, FILING) is not a property of the item: the
same headline is DIRECT for MSFT and PEER for AAPL, so it is worked out per ticker by
`relation_for` using the relationship map.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..relationships import Relationships

DIRECT = "DIRECT"
PEER = "PEER"
SECTOR = "SECTOR"
MACRO = "MACRO"
FILING = "FILING"
RELATIONS = (DIRECT, PEER, SECTOR, MACRO, FILING)

KIND_NEWS = "news"
KIND_FILING = "filing"


def parse_when(value: Any) -> Optional[datetime]:
    """ISO text, a unix number or a datetime -> an aware UTC datetime. None when it cannot be read."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


@dataclass
class NewsItem:
    id: str                      # "alpaca:12345", "finnhub:987", "edgar:0000320193-26-000123"
    source: str                  # "alpaca/benzinga", "finnhub/Reuters", "edgar/8-K", "edgar/Form 4"
    headline: str
    published_at: datetime       # aware, UTC
    url: str = ""
    summary: str = ""
    symbols: list[str] = field(default_factory=list)   # tickers the source attached, upper case
    kind: str = KIND_NEWS
    catalyst: str = "other"
    sentiment: Optional[float] = None                  # P(positive) - P(negative), from the scorer
    sentiment_note: str = ""                           # why sentiment is missing, or the model name
    duplicate_of: Optional[str] = None                 # id of the item this one repeats
    first_seen: Optional[datetime] = None

    def __post_init__(self) -> None:
        self.symbols = list(dict.fromkeys(s.upper() for s in self.symbols if s))
        self.headline = " ".join(str(self.headline).split())
        if self.published_at.tzinfo is None:
            self.published_at = self.published_at.replace(tzinfo=timezone.utc)

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "kind": self.kind,
            "headline": self.headline,
            "summary": self.summary,
            "url": self.url,
            "published_at": iso(self.published_at),
            "symbols": json.dumps(self.symbols),
            "catalyst": self.catalyst,
            "sentiment": self.sentiment,
            "sentiment_note": self.sentiment_note,
            "duplicate_of": self.duplicate_of,
            "first_seen": iso(self.first_seen),
        }

    @classmethod
    def from_row(cls, row: Any) -> "NewsItem":
        return cls(
            id=row["id"],
            source=row["source"],
            kind=row["kind"],
            headline=row["headline"],
            summary=row["summary"] or "",
            url=row["url"] or "",
            published_at=parse_when(row["published_at"]),
            symbols=json.loads(row["symbols"] or "[]"),
            catalyst=row["catalyst"] or "other",
            sentiment=row["sentiment"],
            sentiment_note=row["sentiment_note"] or "",
            duplicate_of=row["duplicate_of"],
            first_seen=parse_when(row["first_seen"]),
        )


def relation_for(item: NewsItem, ticker: str, rmap: Relationships) -> Optional[str]:
    """How this headline relates to the ticker, or None when it does not. Precedence: FILING, DIRECT, PEER, SECTOR, MACRO."""
    ticker = ticker.upper()
    tmap = rmap.for_ticker(ticker)
    if item.kind == KIND_FILING:
        return FILING if ticker in item.symbols else None
    text = item.headline
    if ticker in item.symbols or rmap.mentions(text, ticker):
        return DIRECT
    for peer in tmap.peers:
        if peer in item.symbols or rmap.mentions(text, peer):
            return PEER
    if tmap.sector_etf and (tmap.sector_etf in item.symbols or tmap.sector_etf in rmap.sectors_in(text)):
        return SECTOR
    if tmap.topics and set(rmap.topics_in(text)) & set(tmap.topics):
        return MACRO
    return None


@dataclass
class Reaction:
    """Price at the headline and 1 hour, 1 day and 5 days later. Prices stay None until the time has passed and a bar exists."""
    news_id: str
    symbol: str
    relation: str
    catalyst: str
    published_at: datetime
    p0: Optional[float] = None
    p0_at: Optional[datetime] = None
    p1h: Optional[float] = None
    p1h_at: Optional[datetime] = None
    p1d: Optional[float] = None
    p1d_at: Optional[datetime] = None
    p5d: Optional[float] = None
    p5d_at: Optional[datetime] = None
    status: str = "pending"      # pending, done, failed
    note: str = ""
    next_try_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def move(self, which: str) -> Optional[float]:
        """Percent move from p0 to p1h / p1d / p5d, or None when either price is missing."""
        later = getattr(self, which)
        if self.p0 is None or later is None or self.p0 == 0:
            return None
        return (later / self.p0 - 1.0) * 100.0

    def is_complete(self) -> bool:
        return all(getattr(self, k) is not None for k in ("p0", "p1h", "p1d", "p5d"))


@dataclass
class FeedEntry:
    item: NewsItem
    relation: str
    reaction: Optional[Reaction] = None


@dataclass
class NewsBoard:
    """What one news refresh produced: a feed per watchlist ticker plus the state of every source."""
    feeds: dict[str, list[FeedEntry]] = field(default_factory=dict)
    source_status: dict[str, str] = field(default_factory=dict)   # "alpaca news" -> "ok, 50 items" / "MISSING: no key"
    scorer_status: str = ""
    map_notes: dict[str, str] = field(default_factory=dict)       # ticker -> note when the map has no entry
    new_items: int = 0
    duplicates: int = 0
    refreshed_at: Optional[datetime] = None
