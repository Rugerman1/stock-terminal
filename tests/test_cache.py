import pytest

from stock_terminal.cache import Cache


def test_round_trip_and_ttl(tmp_path, clock):
    cache = Cache(tmp_path / "x.db", clock=clock)
    cache.set("k", {"a": 1}, ttl=60)
    entry = cache.get("k")
    assert entry.value == {"a": 1}
    assert entry.fetched_at == 1_000.0
    assert entry.is_fresh(1_030.0)
    assert entry.is_fresh(1_060.0)
    assert not entry.is_fresh(1_061.0)
    assert entry.age(1_010.0) == 10.0


def test_missing_key(cache):
    assert cache.get("nope") is None


def test_replace_delete_count(cache):
    cache.set("k", 1, 10)
    cache.set("k", 2, 10)
    assert cache.count() == 1
    assert cache.get("k").value == 2
    cache.delete("k")
    assert cache.count() == 0
    assert cache.get("k") is None


def test_non_json_value_is_a_bug_not_silently_dropped(cache):
    with pytest.raises(TypeError):
        cache.set("k", object(), 10)


def test_now_uses_injected_clock(cache, clock):
    clock.t = 5.0
    assert cache.now() == 5.0


def test_creates_parent_folder(tmp_path):
    cache = Cache(tmp_path / "deep" / "er" / "c.db")
    cache.set("k", 1, 1)
    assert (tmp_path / "deep" / "er" / "c.db").exists()
