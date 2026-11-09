"""How often one key may call one route, counted in this process's memory.

The watch routes are the gateway's only routes a caller reaches without any credential
-- a watch asking for a pairing code has nothing to present yet -- so they are counted
per source address, and an accepted watch is counted per account as well. Nothing here
is written anywhere: a restart forgets every count, which is the right failure for a
limit whose only job is to keep one caller from crowding out the rest.

A token bucket per key: ``limit`` calls may arrive at once, and one more is allowed every
``window_seconds / limit`` after that. The number of keys held is bounded; past it the
least recently seen key is forgotten, which hands that key a full bucket again rather than
growing without end under a flood of fresh addresses.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable


class RateLimiter:
    """``allow(key)`` answers ``None`` and counts the call, or the seconds to wait."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        if not isinstance(window_seconds, (int, float)) or window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if type(max_keys) is not int or max_keys < 1:
            raise ValueError("max_keys must be a positive integer")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self._rate = limit / self.window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> int | None:
        """``None`` when this call is within the limit; otherwise whole seconds to wait."""
        now = self._clock()
        with self._lock:
            tokens, seen = self._buckets.pop(key, (float(self.limit), now))
            tokens = min(float(self.limit), tokens + max(now - seen, 0.0) * self._rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                wait = None
            else:
                self._buckets[key] = (tokens, now)
                wait = max(1, math.ceil((1.0 - tokens) / self._rate))
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
            return wait
