import json
from datetime import datetime, timedelta, timezone

import httpx

from stock_terminal.clients.finnhub_client import NO_KEY, FinnhubClient, items_from_payload
from stock_terminal.config import Keys
from stock_terminal.datapoint import FRESH, MISSING

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
STAMP = int(NOW.timestamp())


def test_items_from_payload_newest_first_and_limited():
    payload = [
        {"id": 1, "datetime": STAMP - 3600, "headline": "older", "url": "", "source": "Reuters", "related": "AAPL,MSFT"},
        {"id": 2, "datetime": STAMP, "headline": "newer", "url": "", "source": "Yahoo", "related": ""},
        {"id": 3, "datetime": STAMP - 7200, "headline": "oldest", "url": "", "source": "CNBC", "related": "NVDA"},
    ]
    items = items_from_payload(payload, limit=2)
    assert [i.id for i in items] == ["finnhub:2", "finnhub:1"]
    assert items[1].symbols == ["AAPL", "MSFT"] and items[0].symbols == [] and items[0].source == "finnhub/Yahoo"


def make_client(cfg, cache, key="k"):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers.get("X-Finnhub-Token") == key
        assert "token" not in str(request.url)
        path = request.url.path
        if path.endswith("/company-news"):
            body = [{"id": 7, "datetime": STAMP - 60, "headline": "Apple beats", "url": "", "source": "Reuters", "related": "AAPL", "summary": "s"}]
        elif path.endswith("/news"):
            body = [{"id": 8, "datetime": STAMP - 120, "headline": "Fed holds rates", "url": "", "source": "CNBC", "related": "", "summary": ""}]
        elif path.endswith("/stock/peers"):
            body = ["AAPL", "MSFT", "DELL", "HPQ"]
        elif path.endswith("/calendar/earnings"):
            assert request.url.params["symbol"] == "AAPL" and request.url.params["from"] == "2026-11-20"
            body = {"earningsCalendar": [{"date": "2027-01-27", "hour": "amc"}, {"date": "2026-11-02", "hour": "amc"}]}
        elif path.endswith("/stock/metric"):
            assert request.url.params["metric"] == "all"
            body = {"metric": {"epsTTM": 8.97, "peTTM": 37.6, "roeTTM": 127.6, "grossMarginTTM": 48.79, "52WeekHigh": 400.0, "pfcfShareTTM": None}, "series": {}}
        else:
            return httpx.Response(404)
        return httpx.Response(200, content=json.dumps(body))

    client = FinnhubClient(Keys(finnhub=key), cache, cfg, transport=httpx.MockTransport(handler))
    return client, seen


def test_company_general_and_peers(cfg, cache):
    client, seen = make_client(cfg, cache)
    items, dp = client.company_news("aapl", NOW - timedelta(days=7), NOW)
    assert dp.status == FRESH and items[0].id == "finnhub:7" and items[0].symbols == ["AAPL"]
    assert seen[0].url.params["symbol"] == "AAPL" and seen[0].url.params["from"] == "2026-11-13"
    items, dp = client.general_news()
    assert items[0].headline == "Fed holds rates" and items[0].symbols == []
    peers = client.peers("AAPL")
    assert peers.ok and peers.value == ["MSFT", "DELL", "HPQ"]       # the symbol itself is dropped
    client.company_news("AAPL", NOW - timedelta(days=7), NOW)
    assert len(seen) == 3                                             # cached inside the TTL


def test_metrics_keep_only_the_fields_the_score_uses(cfg, cache):
    client, seen = make_client(cfg, cache)
    dp = client.metrics("aapl")
    assert dp.ok and dp.value == {"epsTTM": 8.97, "peTTM": 37.6, "roeTTM": 127.6, "grossMarginTTM": 48.79}
    client.metrics("AAPL")
    assert len(seen) == 1                                             # cached for a day
    assert FinnhubClient(Keys(), cache, cfg).metrics("AAPL").status == MISSING


def test_next_earnings_is_the_first_date_on_or_after_today(cfg, cache):
    from datetime import date
    client, seen = make_client(cfg, cache)
    dp = client.next_earnings("aapl", today=date(2026, 11, 20))
    assert dp.ok and dp.value == "2027-01-27" and dp.as_of.date().isoformat() == "2027-01-27"
    client.next_earnings("AAPL", today=date(2026, 11, 20))
    assert len(seen) == 1
    assert FinnhubClient(Keys(), cache, cfg).next_earnings("AAPL").status == MISSING


def test_without_key_is_missing(cfg, cache):
    client = FinnhubClient(Keys(), cache, cfg)
    items, dp = client.company_news("AAPL", NOW, NOW)
    assert items == [] and dp.status == MISSING and dp.note == NO_KEY
    assert client.peers("AAPL").status == MISSING


def test_http_error_becomes_missing_with_reason(cfg, cache):
    client = FinnhubClient(Keys(finnhub="k"), cache, cfg, transport=httpx.MockTransport(lambda r: httpx.Response(429, text="limit")))
    items, dp = client.general_news()
    assert items == [] and dp.status == MISSING and "429" in dp.note
