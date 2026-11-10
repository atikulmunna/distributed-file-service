"""Per-user request rate limiting.

The limit is per user (the authenticated principal), not per credential, so refreshing a token or
rotating an API key does not buy a new budget. Backends:
  memory  per instance: each instance allows the full limit (fine for one instance).
  redis   shared by all instances: a fixed window counter per user (INCR plus EXPIRE on
          rl:<user>:<window>), so the limit holds across instances.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from app.config import settings


class RateLimiter:
    def allow(self, principal: str, limit: int, window_s: int) -> bool:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError


class InMemoryRateLimiter(RateLimiter):
    """Sliding window per user. Users idle for a whole window are dropped, so memory stays bounded."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}
        self._next_sweep = 0.0

    def allow(self, principal: str, limit: int, window_s: int) -> bool:
        if limit <= 0:
            return True
        now = self._clock()
        cutoff = now - window_s
        with self._lock:
            if now >= self._next_sweep:
                self._events = {
                    user: events for user, events in self._events.items() if events and events[-1] >= cutoff
                }
                self._next_sweep = now + window_s
            events = self._events.setdefault(principal, deque())
            while events and events[0] < cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True

    def tracked_principals(self) -> int:
        with self._lock:
            return len(self._events)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._next_sweep = 0.0


class RedisRateLimiter(RateLimiter):
    """Fixed window shared by every instance."""

    def __init__(self, redis_url: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)

    def allow(self, principal: str, limit: int, window_s: int) -> bool:
        if limit <= 0:
            return True
        key = f"rl:{principal}:{int(time.time() // window_s)}"
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_s + 1)
        count, _ = pipe.execute()
        return count <= limit

    def reset(self) -> None:
        for key in self._redis.scan_iter("rl:*"):
            self._redis.delete(key)


def build_rate_limiter() -> RateLimiter:
    backend = settings.rate_limit_backend.lower()
    if backend == "memory":
        return InMemoryRateLimiter()
    if backend == "redis":
        return RedisRateLimiter(settings.redis_url)
    raise ValueError(f"unsupported rate_limit_backend: {settings.rate_limit_backend}")


rate_limiter = build_rate_limiter()
