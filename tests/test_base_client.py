from stock_terminal.clients.base import BaseClient, short_error
from stock_terminal.datapoint import FRESH, MISSING, STALE
from stock_terminal.ratelimit import RateLimiter


def make_client(cache, clock):
    return BaseClient(cache, RateLimiter(100, 60, clock=clock, sleep=clock.sleep))


def test_fresh_fetch_is_stored(cache, clock):
    client = make_client(cache, clock)
    dp = client.fetch_cached("k", 60, lambda: {"v": 1}, "src")
    assert dp.status == FRESH and dp.value == {"v": 1} and dp.source == "src"
    assert cache.get("k").value == {"v": 1}


def test_served_from_cache_inside_ttl(cache, clock):
    client = make_client(cache, clock)
    calls = []
    fetch = lambda: calls.append(1) or {"v": 1}  # noqa: E731
    client.fetch_cached("k", 60, fetch, "src")
    clock.t += 59
    dp = client.fetch_cached("k", 60, fetch, "src")
    assert len(calls) == 1 and dp.status == FRESH and dp.note == "cache"


def test_refetches_after_ttl(cache, clock):
    client = make_client(cache, clock)
    values = iter([{"v": 1}, {"v": 2}])
    client.fetch_cached("k", 60, lambda: next(values), "src")
    clock.t += 61
    dp = client.fetch_cached("k", 60, lambda: next(values), "src")
    assert dp.value == {"v": 2} and dp.status == FRESH


def test_stale_when_source_fails_after_ttl(cache, clock):
    client = make_client(cache, clock)
    client.fetch_cached("k", 60, lambda: {"v": 1}, "src")
    clock.t += 120

    def boom():
        raise RuntimeError("boom")

    dp = client.fetch_cached("k", 60, boom, "src")
    assert dp.status == STALE and dp.value == {"v": 1}
    assert "RuntimeError: boom" in dp.note and "2m ago" in dp.note


def test_missing_when_nothing_cached_and_source_fails(cache, clock):
    client = make_client(cache, clock)

    def boom():
        raise ConnectionError("no route")

    dp = client.fetch_cached("k", 60, boom, "src")
    assert dp.status == MISSING and dp.value is None and "ConnectionError: no route" in dp.note


def test_none_from_source_is_missing(cache, clock):
    client = make_client(cache, clock)
    dp = client.fetch_cached("k", 60, lambda: None, "src")
    assert dp.status == MISSING and dp.note == "source returned nothing"


def test_short_error_scrubs_api_key_and_trims():
    text = short_error(RuntimeError("GET https://api.example/obs?series_id=X&api_key=SUPERSECRET&file_type=json failed"))
    assert "SUPERSECRET" not in text and "api_key=***" in text
    assert len(short_error(RuntimeError("x" * 500))) <= 140
