"""Storage circuit breaker: after repeated transient failures, fail fast instead of tying up workers."""

import threading
import time

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi import HTTPException

from app.config import settings
from app.storage import storage
from app.storage_policy import BreakerOpenError, StorageCircuitBreaker, storage_breaker


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _breaker(threshold: int = 3, open_seconds: float = 10.0):
    clock = FakeClock()
    return StorageCircuitBreaker(failure_threshold=threshold, open_seconds=open_seconds, clock=clock), clock


def test_breaker_opens_after_consecutive_transient_failures() -> None:
    breaker, clock = _breaker(threshold=3)
    for _ in range(2):
        breaker.before_call()
        breaker.record_failure(transient=True)
    assert breaker.state == "closed"
    breaker.before_call()
    breaker.record_failure(transient=True)
    assert breaker.state == "open"
    with pytest.raises(BreakerOpenError) as raised:
        breaker.before_call()
    assert raised.value.retry_after_s == pytest.approx(10.0)
    clock.now += 4
    with pytest.raises(BreakerOpenError) as raised:
        breaker.before_call()
    assert raised.value.retry_after_s == pytest.approx(6.0)


def test_success_resets_the_failure_count() -> None:
    breaker, _ = _breaker(threshold=3)
    for _ in range(2):
        breaker.record_failure(transient=True)
    breaker.record_success()
    for _ in range(2):
        breaker.record_failure(transient=True)
    assert breaker.state == "closed"


def test_permanent_errors_do_not_trip_breaker() -> None:
    breaker, _ = _breaker(threshold=2)
    for _ in range(5):
        breaker.before_call()
        breaker.record_failure(transient=False)  # storage answered: it is up
    assert breaker.state == "closed"


def test_breaker_half_open_probe_success_closes() -> None:
    breaker, clock = _breaker(threshold=1, open_seconds=10)
    breaker.record_failure(transient=True)
    clock.now += 10
    breaker.before_call()  # the single probe is let through
    assert breaker.state == "half_open"
    with pytest.raises(BreakerOpenError):
        breaker.before_call()  # only one probe at a time
    breaker.record_success()
    assert breaker.state == "closed"
    breaker.before_call()


def test_breaker_half_open_probe_failure_reopens() -> None:
    breaker, clock = _breaker(threshold=1, open_seconds=10)
    breaker.record_failure(transient=True)
    clock.now += 10
    breaker.before_call()
    breaker.record_failure(transient=True)
    assert breaker.state == "open"
    with pytest.raises(BreakerOpenError) as raised:
        breaker.before_call()
    assert raised.value.retry_after_s == pytest.approx(10.0)


def test_breaker_opens_after_threshold_and_fails_fast(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_retries", 0)
    monkeypatch.setattr(storage_breaker, "failure_threshold", 3)
    calls = {"n": 0}

    def unreachable(*args, **kwargs):
        calls["n"] += 1
        time.sleep(0.05)  # a failing attempt costs time
        raise EndpointConnectionError(endpoint_url="http://s3")

    monkeypatch.setattr(storage, "write_chunk", unreachable)
    init = client.post(
        "/v1/uploads/init", json={"file_name": "f.bin", "file_size": 5, "chunk_size": 5}, headers=auth_headers
    )
    upload_id = init.json()["upload_id"]
    for _ in range(3):
        assert (
            client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"aaaaa", headers=auth_headers).status_code == 503
        )
    assert calls["n"] == 3

    started = time.perf_counter()
    fast = client.put(f"/v1/uploads/{upload_id}/chunks/0", content=b"aaaaa", headers=auth_headers)
    elapsed = time.perf_counter() - started
    assert fast.status_code == 503
    assert calls["n"] == 3  # storage was not called at all
    assert 1 <= int(fast.headers["Retry-After"]) <= settings.breaker_open_seconds
    assert elapsed < 0.1


def test_breaker_stops_retries_once_it_opens(monkeypatch) -> None:
    from app.storage_policy import with_storage_retries

    monkeypatch.setattr(settings, "max_retries", 10)
    monkeypatch.setattr(settings, "retry_base_delay_ms", 1)
    monkeypatch.setattr(storage_breaker, "failure_threshold", 3)
    calls = {"n": 0}

    def unreachable():
        calls["n"] += 1
        raise EndpointConnectionError(endpoint_url="http://s3")

    with pytest.raises(HTTPException) as raised:
        with_storage_retries(unreachable, "test")
    assert calls["n"] == 3  # the breaker opened on the third failure; the remaining retries were skipped
    assert raised.value.status_code == 503


def test_a_permanent_error_through_the_policy_keeps_the_breaker_closed(monkeypatch) -> None:
    from app.storage_policy import with_storage_retries

    monkeypatch.setattr(storage_breaker, "failure_threshold", 1)
    denied = ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject")

    def forbidden():
        raise denied

    for _ in range(3):
        with pytest.raises(HTTPException) as raised:
            with_storage_retries(forbidden, "test")
        assert raised.value.status_code == 500
    assert storage_breaker.state == "closed"


def test_concurrent_callers_get_one_probe(monkeypatch) -> None:
    breaker, clock = _breaker(threshold=1, open_seconds=1)
    breaker.record_failure(transient=True)
    clock.now += 1
    let_through: list[bool] = []

    def caller() -> None:
        try:
            breaker.before_call()
            let_through.append(True)
        except BreakerOpenError:
            let_through.append(False)

    threads = [threading.Thread(target=caller) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert let_through.count(True) == 1
