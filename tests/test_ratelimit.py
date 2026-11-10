"""Per-user rate limiting: keyed by user (not by credential), and no unbounded memory growth."""

import time

import jwt

from app.config import settings
from app.ratelimit import InMemoryRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_in_memory_limit_per_window() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock)
    assert [limiter.allow("u1", 2, 60) for _ in range(3)] == [True, True, False]
    assert limiter.allow("u2", 2, 60) is True  # another user has their own budget
    clock.now += 61
    assert limiter.allow("u1", 2, 60) is True


def test_zero_limit_means_unlimited() -> None:
    limiter = InMemoryRateLimiter()
    assert all(limiter.allow("u1", 0, 60) for _ in range(100))


def test_rate_limiter_drops_empty_buckets() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock)
    for user in ("u1", "u2", "u3"):
        limiter.allow(user, 5, 60)
    assert limiter.tracked_principals() == 3
    clock.now += 61  # everyone's window has passed
    limiter.allow("u4", 5, 60)
    assert limiter.tracked_principals() == 1


def _token(sub: str) -> str:
    # a distinct token per call (different jti), as a client refreshing its token would get
    payload = {"sub": sub, "jti": str(time.perf_counter_ns()), "exp": int(time.time()) + 300}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def test_rate_limit_keyed_by_user_not_token(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_mode", "jwt")
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-test-secret-test-secret")
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 2)
    statuses = [
        client.get("/v1/uploads/no-such-upload", headers={"Authorization": f"Bearer {_token('user-a')}"}).status_code
        for _ in range(3)
    ]
    assert statuses[:2] == [404, 404]
    assert statuses[2] == 429  # new tokens do not buy a new budget
