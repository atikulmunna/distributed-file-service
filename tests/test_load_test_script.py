import httpx

from scripts.load_test import RunStats, percentile, send_with_retry


def _sequence(*responses: httpx.Response):
    remaining = list(responses)
    return lambda: remaining.pop(0)


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
    assert percentile(values, 99) == 99.0
    assert percentile([7.0], 99) == 7.0
    assert percentile([], 95) == 0.0


def test_send_with_retry_honors_retry_after() -> None:
    stats = RunStats()
    sleeps: list[float] = []
    response = send_with_retry(
        _sequence(httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(202)),
        stats,
        sleep=sleeps.append,
    )
    assert response.status_code == 202
    assert sleeps == [2.0]
    assert stats.retries == 1
    assert dict(stats.errors_by_status) == {}


def test_send_with_retry_caps_wait_and_attempts() -> None:
    stats = RunStats()
    sleeps: list[float] = []
    response = send_with_retry(
        lambda: httpx.Response(503, headers={"Retry-After": "99"}),
        stats,
        max_attempts=3,
        max_wait_s=10.0,
        sleep=sleeps.append,
    )
    assert response.status_code == 503
    assert sleeps == [10.0, 10.0]
    assert stats.retries == 2
    assert dict(stats.errors_by_status) == {"503": 1}


def test_send_with_retry_does_not_retry_other_errors() -> None:
    stats = RunStats()
    sleeps: list[float] = []
    response = send_with_retry(lambda: httpx.Response(500), stats, sleep=sleeps.append)
    assert response.status_code == 500
    assert sleeps == []
    assert stats.retries == 0
    assert dict(stats.errors_by_status) == {"500": 1}
