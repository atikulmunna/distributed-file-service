import pytest
from fastapi import HTTPException

from app.limits import PerUploadInflightLimiter


def test_fair_share_limit_throttles_single_upload() -> None:
    limiter = PerUploadInflightLimiter(limit=10, fair_share_limit=2)
    limiter.acquire("u1")
    limiter.acquire("u1")

    with pytest.raises(HTTPException) as exc_info:
        limiter.acquire("u1")

    exc = exc_info.value
    assert exc.status_code == 429
    assert exc.headers is not None
    assert exc.headers.get("X-RateLimit-Reason") == "upload_fair_share_limit"


def test_hard_limit_takes_priority_over_fair_share() -> None:
    limiter = PerUploadInflightLimiter(limit=1, fair_share_limit=5)
    limiter.acquire("u1")

    with pytest.raises(HTTPException) as exc_info:
        limiter.acquire("u1")

    exc = exc_info.value
    assert exc.status_code == 429
    assert exc.headers is not None
    assert exc.headers.get("X-RateLimit-Reason") == "upload_inflight_limit"
