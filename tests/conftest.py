import pytest

from stock_terminal.cache import Cache
from stock_terminal.config import load_config


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
    # A path that does not exist gives the built-in defaults.
    return load_config(tmp_path / "no-such-config.yaml")
