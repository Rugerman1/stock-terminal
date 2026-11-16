import pytest

from stock_terminal.ratelimit import RateLimiter


def test_burst_then_wait(clock):
    limiter = RateLimiter(3, 60, clock=clock, sleep=clock.sleep)
    assert [limiter.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert limiter.in_window() == 3
    waited = limiter.acquire()  # the fourth call must wait for the first slot to age out
    assert waited == pytest.approx(60.0)
    assert clock.t == pytest.approx(1_060.0)


def test_window_slides(clock):
    limiter = RateLimiter(2, 60, clock=clock, sleep=clock.sleep)
    limiter.acquire()
    clock.t += 30
    limiter.acquire()
    clock.t += 31  # first call is now 61 s old, the second 31 s old
    assert limiter.acquire() == 0.0
    assert limiter.in_window() == 2


def test_per_minute_helper(clock):
    limiter = RateLimiter.per_minute(150, clock=clock, sleep=clock.sleep)
    assert limiter.max_calls == 150 and limiter.period == 60.0


def test_validation():
    with pytest.raises(ValueError):
        RateLimiter(0, 60)
    with pytest.raises(ValueError):
        RateLimiter(1, 0)
