from datetime import datetime, timedelta, timezone

from stock_terminal.clients.alpaca_client import NO_KEY
from stock_terminal.clients.alpaca_news import AlpacaNewsClient, items_from_payload
from stock_terminal.config import Keys
from stock_terminal.datapoint import FRESH, MISSING

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


def test_items_from_payload_parses_and_skips_broken_rows():
    payload = [
        {"id": 1, "headline": "  Apple  beats ", "summary": "s", "url": "u", "source": "Benzinga", "created_at": NOW.isoformat(), "symbols": ["AAPL", "aapl"]},
        {"id": 2, "headline": "", "created_at": NOW.isoformat()},
        {"id": None, "headline": "no id", "created_at": NOW.isoformat()},
        {"id": 3, "headline": "bad date", "created_at": "yesterday"},
    ]
    items = items_from_payload(payload)
    assert len(items) == 1
    item = items[0]
    assert item.id == "alpaca:1" and item.source == "alpaca/benzinga" and item.headline == "Apple beats"
    assert item.symbols == ["AAPL"] and item.published_at == NOW and item.kind == "news"


class FakeNewsClient:
    def __init__(self):
        self.calls = []

    def get_news(self, request):
        self.calls.append(request)
        return {
            "news": [
                {"id": 10, "headline": "Nvidia earnings beat", "summary": "", "url": "https://x/10", "source": "benzinga",
                 "created_at": NOW - timedelta(hours=1), "updated_at": NOW, "symbols": ["NVDA"]},
            ]
        }


def test_news_with_fake_client_is_cached(cfg, cache):
    fake = FakeNewsClient()
    client = AlpacaNewsClient(Keys(), cache, cfg, news_client=fake)
    items, dp = client.news(["nvda", "AMD"], NOW - timedelta(days=7))
    assert dp.status == FRESH and len(items) == 1 and items[0].id == "alpaca:10" and items[0].symbols == ["NVDA"]
    assert fake.calls[0].symbols == "AMD,NVDA" and fake.calls[0].limit == 50
    client.news(["AMD", "NVDA"], NOW - timedelta(days=7))
    assert len(fake.calls) == 1                                   # same symbols inside the TTL: served from cache
    items, dp = client.news(None, NOW - timedelta(days=7))        # general news is a separate call
    assert len(fake.calls) == 2 and fake.calls[1].symbols is None


def test_without_key_never_touches_network(cfg, cache):
    client = AlpacaNewsClient(Keys(), cache, cfg)
    items, dp = client.news(["AAPL"], NOW)
    assert items == [] and dp.status == MISSING and dp.note == NO_KEY
