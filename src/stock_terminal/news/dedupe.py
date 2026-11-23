"""Spotting the same story from different outlets.

Two headlines are the same story when they were published within `hours` of each
other and share at least `threshold` of their words (Jaccard similarity of the word
sets after light stemming), or when they point at the same URL. Wire stories that
several outlets re-run pass this test; two genuinely different articles about the
same event usually do not, and that is on purpose: a paraphrase check that is too
loose would hide real news.
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


def normalise_url(url: str) -> str:
    url = (url or "").strip().lower()
    url = re.sub(r"[?#].*$", "", url)        # drop tracking parameters
    url = re.sub(r"^https?://(www\.)?", "", url)
    return url.rstrip("/")


def is_same_story(a: NewsItem, b: NewsItem, hours: float = 48.0, threshold: float = 0.5) -> bool:
    if a.id == b.id:
        return False
    if a.url and b.url and normalise_url(a.url) == normalise_url(b.url):
        return True
    if abs(a.published_at - b.published_at) > timedelta(hours=hours):
        return False
    ta, tb = tokens(a.headline), tokens(b.headline)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    return similarity(ta, tb) >= threshold


def find_original(item: NewsItem, candidates: Iterable[NewsItem], hours: float = 48.0, threshold: float = 0.5) -> Optional[NewsItem]:
    """The earliest already-known item that is the same story, or None. Duplicates of duplicates resolve to the original."""
    best: Optional[NewsItem] = None
    for other in candidates:
        if other.is_duplicate or other.id == item.id:
            continue
        if is_same_story(item, other, hours, threshold):
            if best is None or other.published_at < best.published_at:
                best = other
    return best
