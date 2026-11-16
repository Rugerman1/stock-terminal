"""A rate limiter: at most `max_calls` calls in any rolling window of `period` seconds.

Every data client owns one, sized under its provider's published limit, so a burst
of refreshes never trips a 429 ("too many requests") and gets the key blocked.
The clock and sleep functions are injectable so tests run without waiting.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque


class RateLimiter:
    def __init__(
        self,
        max_calls: int,
        period: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        if period <= 0:
            raise ValueError("period must be positive")
        self.max_calls = max_calls
        self.period = float(period)
        self._clock = clock
        self._sleep = sleep
        self._calls: Deque[float] = deque()
        self._lock = threading.Lock()

    @classmethod
    def per_minute(cls, calls: int, **kwargs) -> "RateLimiter":
        return cls(calls, 60.0, **kwargs)

    def acquire(self) -> float:
        """Block until a call is allowed, then record it. Returns the seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return waited
                wait = self.period - (now - self._calls[0])
            wait = max(wait, 0.001)
            self._sleep(wait)
            waited += wait

    def in_window(self) -> int:
        """How many calls are currently counted against the window (for the --check report)."""
        with self._lock:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self.period:
                self._calls.popleft()
            return len(self._calls)
