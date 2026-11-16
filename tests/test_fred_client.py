from datetime import date

import httpx
import pytest

from stock_terminal.clients.fred_client import NO_KEY, FredClient, parse_latest
from stock_terminal.config import Keys
from stock_terminal.datapoint import FRESH, MISSING, STALE

PAYLOAD = {
    "observations": [
        {"date": "2026-11-13", "value": "."},
        {"date": "2026-11-12", "value": "18.70"},
        {"date": "2026-11-11", "value": "19.01"},
    ]
}


def test_parse_latest_skips_holiday_dots():
    assert parse_latest(PAYLOAD, "VIXCLS") == {"value": 18.70, "date": "2026-11-12"}


def test_parse_latest_sorts_newest_first():
    shuffled = {"observations": list(reversed(PAYLOAD["observations"]))}
    assert parse_latest(shuffled, "VIXCLS")["date"] == "2026-11-12"


def test_parse_latest_raises_when_nothing_numeric():
    with pytest.raises(ValueError):
        parse_latest({"observations": [{"date": "2026-11-13", "value": "."}]}, "VIXCLS")
    with pytest.raises(ValueError):
        parse_latest({}, "VIXCLS")


def test_no_key_is_missing_without_network(cfg, cache):
    dp = FredClient(Keys(), cache, cfg, transport=httpx.MockTransport(lambda r: pytest.fail("must not call")))
    result = dp.latest("VIXCLS")
    assert result.status == MISSING and result.note == NO_KEY and result.source == "fred/VIXCLS"


def test_latest_parses_value_and_date(cfg, cache):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=PAYLOAD)

    client = FredClient(Keys(fred="k123"), cache, cfg, transport=httpx.MockTransport(handler))
    dp = client.latest("VIXCLS")
    assert dp.status == FRESH and dp.value == 18.70
    assert dp.as_of.date() == date(2026, 11, 12)
    assert seen["params"]["series_id"] == "VIXCLS" and seen["params"]["file_type"] == "json"
    assert seen["params"]["api_key"] == "k123"


def test_http_error_is_missing_and_never_leaks_the_key(cfg, cache):
    handler = lambda request: httpx.Response(500, text="server fell over")  # noqa: E731
    client = FredClient(Keys(fred="SUPERSECRET"), cache, cfg, transport=httpx.MockTransport(handler))
    dp = client.latest("DGS10")
    assert dp.status == MISSING
    assert "500" in dp.note and "SUPERSECRET" not in dp.note


def test_failure_after_success_serves_stale(cfg, cache, clock):
    responses = iter([httpx.Response(200, json=PAYLOAD), httpx.Response(503, text="down")])
    client = FredClient(Keys(fred="k"), cache, cfg, transport=httpx.MockTransport(lambda r: next(responses)))
    first = client.latest("VIXCLS")
    assert first.status == FRESH
    clock.t += cfg["cache"]["ttl_seconds"]["fred"] + 1
    second = client.latest("VIXCLS")
    assert second.status == STALE and second.value == 18.70 and "503" in second.note
