"""FRED (Federal Reserve Economic Data): the VIX close and the 10-year Treasury yield.

Facts this code relies on (verified 2026-11-15): free key, 120 calls a minute, both
series post the next business day, so the newest observation is yesterday's close
and the screen labels it with its date. FRED's terms ask for the attribution line in
ATTRIBUTION and forbid long-term archiving, so values are cached for hours, not days.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..cache import Cache
from ..config import Keys
from ..datapoint import DataPoint
from ..ratelimit import RateLimiter
from .base import BaseClient

BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
ATTRIBUTION = "This product uses the FRED API but is not endorsed or certified by the Federal Reserve Bank of St. Louis."
NO_KEY = "no FRED key in .env"


def parse_latest(payload: dict[str, Any], series_id: str) -> dict[str, Any]:
    """The newest numeric observation. FRED marks holidays and gaps with '.', which are skipped."""
    observations = payload.get("observations") or []
    ordered = sorted(observations, key=lambda o: o.get("date", ""), reverse=True)
    for obs in ordered:
        raw = obs.get("value")
        if raw in (None, "", "."):
            continue
        try:
            return {"value": float(raw), "date": obs["date"]}
        except (TypeError, ValueError, KeyError):
            continue
    raise ValueError(f"FRED returned no numeric observation for {series_id}")


class FredClient(BaseClient):
    name = "fred"

    def __init__(
        self,
        keys: Keys,
        cache: Cache,
        cfg: dict[str, Any],
        logger: Optional[logging.Logger] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        super().__init__(cache, RateLimiter.per_minute(cfg["fred"]["calls_per_minute"]), logger)
        self.ttl = cfg["cache"]["ttl_seconds"]["fred"]
        self.enabled = keys.has_fred
        self._api_key = keys.fred
        self._http = httpx.Client(timeout=10.0, transport=transport)

    def latest(self, series_id: str) -> DataPoint:
        source = f"fred/{series_id}"
        if not self.enabled:
            return DataPoint.missing(source, NO_KEY)
        dp = self.fetch_cached(f"fred:latest:{series_id}", self.ttl, lambda: self._fetch_latest(series_id), source)
        if not dp.ok:
            return dp
        as_of = datetime.strptime(dp.value["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return DataPoint(
            value=float(dp.value["value"]),
            source=source,
            status=dp.status,
            as_of=as_of,
            fetched_at=dp.fetched_at,
            note=dp.note,
        )

    def _fetch_latest(self, series_id: str) -> dict[str, Any]:
        response = self._http.get(
            BASE_URL,
            params={
                "series_id": series_id,
                "api_key": self._api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 10,
            },
        )
        response.raise_for_status()
        return parse_latest(response.json(), series_id)
