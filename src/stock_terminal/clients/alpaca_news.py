"""Alpaca news (REST v1beta1/news through alpaca-py's NewsClient).

Free plan facts this code relies on (verified 2026-11-15): up to 50 items per call,
history back to 2015, sources Benzinga and fiscal.ai, a `symbols` filter. Whether
the Basic plan's news is real-time or delayed is not documented, so the screen shows
each headline's age instead of assuming. One call per watchlist ticker covers the
ticker, its peers and its sector ETF; one more call with no symbols brings general
news for the MACRO tags.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from ..cache import Cache
from ..config import Keys
from ..datapoint import DataPoint
from ..news.models import KIND_NEWS, NewsItem, parse_when
from ..ratelimit import RateLimiter
from .alpaca_client import NO_KEY
from .base import BaseClient


def _raw_items(result: Any) -> list[dict[str, Any]]:
    """alpaca-py returns a NewsSet (data = {"news": [News, ...]}) or, with raw_data, a dict. Accept both."""
    if isinstance(result, dict):
        raw = result.get("news") or []
    else:
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            raw = data.get("news") or []
        else:
            raw = list(data or [])
    out = []
    for entry in raw:
        if hasattr(entry, "model_dump"):
            entry = entry.model_dump()
        out.append(dict(entry))
    return out


def items_from_payload(payload: Optional[list[dict[str, Any]]]) -> list[NewsItem]:
    items: list[NewsItem] = []
    for raw in payload or []:
        published = parse_when(raw.get("created_at") or raw.get("updated_at"))
        headline = (raw.get("headline") or "").strip()
        if published is None or not headline or raw.get("id") is None:
            continue
        items.append(
            NewsItem(
                id=f"alpaca:{raw['id']}",
                source=f"alpaca/{(raw.get('source') or 'news').strip().lower()}",
                headline=headline,
                published_at=published,
                url=raw.get("url") or "",
                summary=(raw.get("summary") or "").strip(),
                symbols=[str(s) for s in (raw.get("symbols") or [])],
                kind=KIND_NEWS,
            )
        )
    return items


class AlpacaNewsClient(BaseClient):
    name = "alpaca_news"

    def __init__(
        self,
        keys: Keys,
        cache: Cache,
        cfg: dict[str, Any],
        logger: Optional[logging.Logger] = None,
        news_client: Any = None,
    ) -> None:
        super().__init__(cache, RateLimiter.per_minute(cfg["alpaca"]["news_calls_per_minute"]), logger)
        self.ttl = cfg["cache"]["ttl_seconds"]["news"]
        self.limit = min(50, int(cfg["news"]["per_source_limit"]))
        self.source = "alpaca/news"
        self.enabled = keys.has_alpaca or news_client is not None
        self._client = news_client
        if self.enabled and news_client is None:
            self._client = NewsClient(keys.alpaca_key_id, keys.alpaca_secret)

    def news(self, symbols: Optional[list[str]], since: datetime) -> tuple[list[NewsItem], DataPoint]:
        """Headlines for the symbols (None = general news) since the given time. Returns (items, status)."""
        wanted = sorted({s.upper() for s in symbols if s}) if symbols else []
        if not self.enabled:
            return [], DataPoint.missing(self.source, NO_KEY)
        key = "alpaca:news:" + (",".join(wanted) or "*") + ":" + since.strftime("%Y-%m-%dT%H")
        dp = self.fetch_cached(key, self.ttl, lambda: self._fetch(wanted, since), self.source)
        return items_from_payload(dp.value), dp

    def _fetch(self, symbols: list[str], since: datetime) -> list[dict[str, Any]]:
        request = NewsRequest(
            symbols=",".join(symbols) if symbols else None,
            start=since,
            limit=self.limit,
            include_content=False,
        )
        result = self._client.get_news(request)
        out = []
        for raw in _raw_items(result):
            out.append(
                {
                    "id": raw.get("id"),
                    "headline": raw.get("headline"),
                    "summary": raw.get("summary"),
                    "url": raw.get("url"),
                    "source": raw.get("source"),
                    "created_at": _iso(raw.get("created_at")),
                    "updated_at": _iso(raw.get("updated_at")),
                    "symbols": list(raw.get("symbols") or []),
                }
            )
        return out


def _iso(value: Any) -> Optional[str]:
    dt = parse_when(value)
    return dt.isoformat() if dt else None
