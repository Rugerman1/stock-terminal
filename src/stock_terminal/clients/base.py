"""Shared plumbing for every data client.

`fetch_cached` is the one routine all clients use to talk to the outside world:
serve from the cache while the value is inside its TTL; otherwise take a slot from
the rate limiter, fetch, store, and return the value as FRESH; if the fetch fails,
return the last cached value marked STALE, or a MISSING DataPoint that says why.
Network trouble therefore becomes a labelled value on screen, never a crash.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..cache import Cache
from ..datapoint import FRESH, STALE, DataPoint, human_age, utcnow
from ..ratelimit import RateLimiter

# FRED puts the API key in the URL, so an error that echoes the URL would leak it. Scrub it.
_KEY_IN_URL = re.compile(r"(api_key=)[^&\s]+", re.IGNORECASE)


def short_error(exc: BaseException) -> str:
    """A one-line, key-free description of an exception for logs and the Data column."""
    text = f"{type(exc).__name__}: {exc}".strip()
    text = _KEY_IN_URL.sub(r"\1***", text)
    text = " ".join(text.split())
    return text[:140]


class BaseClient:
    name = "base"

    def __init__(self, cache: Cache, limiter: RateLimiter, logger: Optional[logging.Logger] = None) -> None:
        self.cache = cache
        self.limiter = limiter
        self.log = logger or logging.getLogger(f"stock_terminal.{self.name}")

    def fetch_cached(self, key: str, ttl: float, fetcher: Callable[[], Any], source: str) -> DataPoint:
        entry = self.cache.get(key)
        now_ts = self.cache.now()
        if entry is not None and entry.is_fresh(now_ts):
            return DataPoint(
                value=entry.value,
                source=source,
                status=FRESH,
                fetched_at=datetime.fromtimestamp(entry.fetched_at, timezone.utc),
                note="cache",
            )
        try:
            self.limiter.acquire()
            value = fetcher()
        except Exception as exc:  # noqa: BLE001  any provider failure (network, auth, parsing) is handled the same way
            reason = short_error(exc)
            self.log.warning("%s: fetch failed for %s: %s", self.name, key, reason)
            if entry is not None:
                return DataPoint(
                    value=entry.value,
                    source=source,
                    status=STALE,
                    fetched_at=datetime.fromtimestamp(entry.fetched_at, timezone.utc),
                    note=f"{reason}; last good value {human_age(entry.age(now_ts))} ago",
                )
            return DataPoint.missing(source, reason)
        if value is None:
            return DataPoint.missing(source, "source returned nothing")
        self.cache.set(key, value, ttl)
        return DataPoint(value=value, source=source, status=FRESH, fetched_at=utcnow())
