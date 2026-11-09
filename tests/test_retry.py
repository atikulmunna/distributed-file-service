import sys
import types

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from app.config import settings
from app.retry import call_with_retry
from app.storage import S3ChunkStorage, is_transient_storage_error


class Transient(Exception):
    pass


def _flaky(failures: int, exc_type=Transient):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc_type(f"failure {calls['n']}")
        return "ok"

    return fn, calls


def _client_error(code: str, status: int = 400) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "m"}, "ResponseMetadata": {"HTTPStatusCode": status}}, "PutObject")


def test_retry_delays_bounded_and_jittered() -> None:
    fn, calls = _flaky(4)
    sleeps: list[float] = []
    draws = iter([0.5, 0.25, 1.0, 0.0])
    result = call_with_retry(
        fn,
        attempts=5,
        base_delay_s=0.1,
        max_delay_s=0.5,
        is_retryable=lambda exc: isinstance(exc, Transient),
        sleep=sleeps.append,
        rng=lambda: next(draws),
    )
    assert result == "ok"
    assert calls["n"] == 5
    # full jitter: delay n = draw * min(max, base * 2**n); bounds 0.1, 0.2, 0.4, then capped at 0.5
    assert sleeps == pytest.approx([0.05, 0.05, 0.4, 0.0])


def test_retry_delays_never_exceed_their_bound_with_real_randomness() -> None:
    fn, _ = _flaky(6)
    sleeps: list[float] = []
    call_with_retry(fn, attempts=7, base_delay_s=0.1, max_delay_s=1.0, is_retryable=lambda e: True, sleep=sleeps.append)
    bounds = [min(1.0, 0.1 * 2**n) for n in range(6)]
    assert len(sleeps) == 6
    assert all(0 <= delay <= bound for delay, bound in zip(sleeps, bounds))


def test_non_retryable_error_fails_fast() -> None:
    fn, calls = _flaky(1, exc_type=ValueError)
    sleeps: list[float] = []
    with pytest.raises(ValueError):
        call_with_retry(fn, attempts=4, base_delay_s=0.1, max_delay_s=1.0, is_retryable=lambda e: False, sleep=sleeps.append)
    assert calls["n"] == 1
    assert sleeps == []


def test_retry_gives_up_after_attempts_and_reports_each_retry() -> None:
    fn, calls = _flaky(10)
    retried: list[Exception] = []
    with pytest.raises(Transient, match="failure 3"):
        call_with_retry(
            fn, attempts=3, base_delay_s=0.0, max_delay_s=0.0, is_retryable=lambda e: True,
            sleep=lambda s: None, on_retry=retried.append,
        )
    assert calls["n"] == 3
    assert len(retried) == 2


@pytest.mark.parametrize(
    "exc, transient",
    [
        (_client_error("SlowDown", 503), True),
        (_client_error("RequestTimeout", 400), True),
        (_client_error("InternalError", 500), True),
        (_client_error("ServiceUnavailable", 503), True),
        (_client_error("Throttling", 400), True),
        (_client_error("Unknown", 502), True),
        (_client_error("Unknown", 504), True),
        (EndpointConnectionError(endpoint_url="http://s3"), True),
        (ReadTimeoutError(endpoint_url="http://s3"), True),
        (ConnectTimeoutError(endpoint_url="http://s3"), True),
        (ConnectionClosedError(endpoint_url="http://s3"), True),
        (_client_error("AccessDenied", 403), False),
        (_client_error("NoSuchBucket", 404), False),
        (_client_error("InvalidPart", 400), False),
        (OSError("disk full"), False),
        (ValueError("bad"), False),
    ],
)
def test_is_transient_storage_error(exc: Exception, transient: bool) -> None:
    assert is_transient_storage_error(exc) is transient


def test_s3_client_pool_sized_to_workers(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda service, **kw: captured.append(kw) or object()))
    monkeypatch.setattr(settings, "worker_count", 16)

    S3ChunkStorage(bucket="b", region="us-east-1")

    config = captured[0]["config"]
    assert config.max_pool_connections == 56  # storage workers (16) + request threads (40)
    assert config.connect_timeout == 5
    assert config.read_timeout == 60
    # one retry layer: the app's policy; boto makes a single attempt per call
    assert config.retries == {"mode": "standard", "total_max_attempts": 1}
