from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stock_terminal.cache import Cache
from stock_terminal.config import load_config
from stock_terminal.news.models import KIND_NEWS, NewsItem
from stock_terminal.news.store import NewsStore
from stock_terminal.relationships import Relationships

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)   # a Friday, 10:00 AM ET


class FakeClock:
    """A clock tests can push forward by hand. Doubles as the limiter's sleep function."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cache(tmp_path, clock) -> Cache:
    return Cache(tmp_path / "test.db", clock=clock)


@pytest.fixture
def cfg(tmp_path) -> dict:
    # A path that does not exist gives the built-in defaults; the relationship map is the shipped one.
    cfg = load_config(tmp_path / "no-such-config.yaml")
    cfg["news"]["relationships_path"] = str(REPO_ROOT / "relationships.yaml")
    cfg["cache"]["path"] = str(tmp_path / "terminal.db")
    return cfg


@pytest.fixture
def rmap() -> Relationships:
    return Relationships.load(REPO_ROOT / "relationships.yaml")


@pytest.fixture
def store(tmp_path) -> NewsStore:
    return NewsStore(tmp_path / "news.db")


@pytest.fixture
def make_item():
    def _make(
        item_id: str,
        headline: str,
        hours_ago: float = 1.0,
        symbols=(),
        kind: str = KIND_NEWS,
        catalyst: str = "other",
        url: str = "",
        source: str = "alpaca/benzinga",
        now: datetime = NOW,
    ) -> NewsItem:
        return NewsItem(
            id=item_id,
            source=source,
            headline=headline,
            published_at=now - timedelta(hours=hours_ago),
            url=url,
            symbols=list(symbols),
            kind=kind,
            catalyst=catalyst,
        )

    return _make
