"""Spotting the same story from different outlets.

Two headlines are the same story when they point at the same URL, or when they were
published within `hours` of each other, are about at least one company in common
(a shared ticker; items with no tickers are compared on words alone), and share at
least `threshold` of their words (Jaccard similarity of the word sets after light
stemming). Wire stories that several outlets re-run pass this test; two different
articles about the same event usually do not, and that is on purpose.

The ticker rule and the 70 percent threshold come from the first universe pull on
2026-11-22: templated headlines ("Why Apple Stock Rose Today" / "Why Nvidia Stock
Rose Today") share three of five words and were being merged across companies.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Iterable, Optional

from .models import NewsItem

STOPWORDS = {
    "a", "an", "and", "the", "of", "to", "in", "on", "for", "at", "by", "with", "from", "as", "is", "are",
    "was", "were", "be", "it", "its", "this", "that", "these", "those", "after", "amid", "over", "into",
    "vs", "versus", "says", "said", "report", "reports", "news", "update", "stock", "stocks", "shares",
    "inc", "corp", "co", "ltd", "plc",
}
_WORD = re.compile(r"[a-z0-9]+")


def stem(word: str) -> str:
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def tokens(headline: str) -> set[str]:
    words = _WORD.findall(headline.lower())
    return {stem(w) for w in words if w not in STOPWORDS and len(w) > 1}


def story_key(headline: str) -> str:
    """A stable key: the sorted stemmed words. Identical keys are certainly the same story."""
    return " ".join(sorted(tokens(headline)))


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


TRACKING_PARAMS = ("utm", "fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "ref", "source", "cmpid")


def normalise_url(url: str) -> str:
    """Lower case, no scheme or www, no fragment, tracking parameters dropped, the rest of the query kept in order.

    The query must stay: Finnhub's links are redirects like finnhub.io/api/news?id=..., and stripping every query
    (the first version of this function) made 3,683 different headlines one URL on the first universe pull (2026-11-22).
    """
    url = (url or "").strip().lower()
    url = re.sub(r"#.*$", "", url)
    url = re.sub(r"^https?://(www\.)?", "", url)
    path, _, query = url.partition("?")
    kept = [p for p in query.split("&") if p and not p.split("=", 1)[0].startswith(TRACKING_PARAMS)]
    path = path.rstrip("/")
    return path + ("?" + "&".join(sorted(kept)) if kept else "")


DEFAULT_THRESHOLD = 0.7


def is_same_story(a: NewsItem, b: NewsItem, hours: float = 48.0, threshold: float = DEFAULT_THRESHOLD) -> bool:
    if a.id == b.id:
        return False
    if a.url and b.url and normalise_url(a.url) == normalise_url(b.url):
        return True
    if abs(a.published_at - b.published_at) > timedelta(hours=hours):
        return False
    if a.symbols and b.symbols and not (set(a.symbols) & set(b.symbols)):
        return False   # two companies' headlines are two stories even when the words line up
    ta, tb = tokens(a.headline), tokens(b.headline)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    return similarity(ta, tb) >= threshold


def find_original(item: NewsItem, candidates: Iterable[NewsItem], hours: float = 48.0, threshold: float = DEFAULT_THRESHOLD) -> Optional[NewsItem]:
    """The earliest already-known item that is the same story, or None. Duplicates of duplicates resolve to the original."""
    best: Optional[NewsItem] = None
    for other in candidates:
        if other.is_duplicate or other.id == item.id:
            continue
        if is_same_story(item, other, hours, threshold):
            if best is None or other.published_at < best.published_at:
                best = other
    return best
