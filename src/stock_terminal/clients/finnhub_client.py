"""Finnhub: company news, general market news, and the peers list.

Free plan facts this code relies on (verified 2026-11-15): 60 calls a minute and 30
a second; company news goes one year back and covers North American companies;
peers and general news are free. The key travels in a request header, never in the
URL, so an error message can never echo it.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from ..cache import Cache
from ..config import Keys
from ..datapoint import DataPoint
from ..news.models import KIND_NEWS, NewsItem, parse_when
from ..ratelimit import RateLimiter
from .base import BaseClient

BASE_URL = "https://finnhub.io/api/v1"
NO_KEY = "no Finnhub key in .env"


def items_from_payload(payload: Optional[list[dict[str, Any]]], limit: int = 50) -> list[NewsItem]:
    items: list[NewsItem] = []
    for raw in payload or []:
        published = parse_when(raw.get("datetime"))
        headline = (raw.get("headline") or "").strip()
        if published is None or not headline or raw.get("id") is None:
            continue
        related = raw.get("related") or ""
        symbols = [s.strip() for s in str(related).split(",") if s.strip()]
        items.append(
            NewsItem(
                id=f"finnhub:{raw['id']}",
                source=f"finnhub/{(raw.get('source') or 'news').strip()}",
                headline=headline,
                published_at=published,
                url=raw.get("url") or "",
                summary=(raw.get("summary") or "").strip(),
                symbols=symbols,
                kind=KIND_NEWS,
            )
        )
    items.sort(key=lambda i: i.published_at, reverse=True)
    return items[:limit]


class FinnhubClient(BaseClient):
    name = "finnhub"

    def __init__(
        self,
        keys: Keys,
        cache: Cache,
        cfg: dict[str, Any],
        logger: Optional[logging.Logger] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        super().__init__(cache, RateLimiter.per_minute(cfg["finnhub"]["calls_per_minute"]), logger)
        self.ttl_news = cfg["cache"]["ttl_seconds"]["news"]
        self.ttl_peers = cfg["cache"]["ttl_seconds"]["peers"]
        self.limit = int(cfg["news"]["per_source_limit"])
        self.enabled = keys.has_finnhub
        headers = {"X-Finnhub-Token": keys.finnhub} if keys.finnhub else {}
        self._http = httpx.Client(timeout=10.0, transport=transport, headers=headers)

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = self._http.get(f"{BASE_URL}{path}", params=params)
        response.raise_for_status()
        return response.json()

    def company_news(self, symbol: str, since: datetime, until: datetime) -> tuple[list[NewsItem], DataPoint]:
        symbol = symbol.upper()
        source = "finnhub/company-news"
        if not self.enabled:
            return [], DataPoint.missing(source, NO_KEY)
        key = f"finnhub:company-news:{symbol}:{since.date()}:{until.date()}"
        params = {"symbol": symbol, "from": since.date().isoformat(), "to": until.date().isoformat()}
        dp = self.fetch_cached(key, self.ttl_news, lambda: self._trim(self._get("/company-news", params)), source)
        return items_from_payload(dp.value, self.limit), dp

    def general_news(self) -> tuple[list[NewsItem], DataPoint]:
        source = "finnhub/general"
        if not self.enabled:
            return [], DataPoint.missing(source, NO_KEY)
        dp = self.fetch_cached(
            "finnhub:news:general", self.ttl_news, lambda: self._trim(self._get("/news", {"category": "general"})), source
        )
        return items_from_payload(dp.value, self.limit), dp

    def peers(self, symbol: str) -> DataPoint:
        symbol = symbol.upper()
        source = "finnhub/peers"
        if not self.enabled:
            return DataPoint.missing(source, NO_KEY)
        return self.fetch_cached(
            f"finnhub:peers:{symbol}",
            self.ttl_peers,
            lambda: [str(p).upper() for p in self._get("/stock/peers", {"symbol": symbol}) if str(p).upper() != symbol],
            source,
        )

    def _trim(self, payload: Any) -> list[dict[str, Any]]:
        """Keep only the fields the app uses, newest first, at most `limit` items, so the cache stays small."""
        if not isinstance(payload, list):
            raise ValueError(f"Finnhub returned {type(payload).__name__}, expected a list")
        rows = sorted((r for r in payload if isinstance(r, dict)), key=lambda r: r.get("datetime") or 0, reverse=True)
        keep = ("id", "datetime", "headline", "summary", "url", "source", "related", "category")
        return [{k: r.get(k) for k in keep} for r in rows[: self.limit]]
