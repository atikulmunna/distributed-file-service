"""Process-level behavior: graceful shutdown of the storage pool and bounded metric labels."""

import os
import socket
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import settings
from app.main import app
from app.worker import BackpressureExecutor


def _pool() -> BackpressureExecutor:
    return BackpressureExecutor(workers=2, queue_maxsize=8, global_inflight_limit=8)


def test_drain_waits_for_inflight_storage_work() -> None:
    pool = _pool()
    finished = threading.Event()

    def slow_write() -> None:
        time.sleep(0.3)
        finished.set()

    pool.submit(slow_write)
    pool.submit(slow_write)
    assert pool.drain(timeout_s=5) == 0
    assert finished.is_set()


def test_drain_gives_up_after_the_timeout_and_reports_what_is_left() -> None:
    pool = _pool()
    release = threading.Event()
    pool.submit(release.wait, 5)
    started = time.monotonic()
    assert pool.drain(timeout_s=0.2) == 1
    assert time.monotonic() - started < 2
    release.set()
    assert pool.drain(timeout_s=5) == 0


def test_drain_of_an_idle_pool_returns_at_once() -> None:
    started = time.monotonic()
    assert _pool().drain(timeout_s=5) == 0
    assert time.monotonic() - started < 0.5


def test_shutdown_drains_the_storage_pool(monkeypatch) -> None:
    calls: list[float] = []
    monkeypatch.setattr(main_module.executor, "drain", lambda timeout_s: calls.append(timeout_s) or 0)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert calls == []  # only on shutdown
    assert calls == [settings.shutdown_drain_seconds]


@pytest.mark.parametrize(("name", "prefix"), [("web-1", "web-1-"), ("", None)])
def test_instance_name_can_replace_the_hostname(name, prefix) -> None:
    # The instance ID is fixed at import, so check it in a fresh interpreter.
    env = {**os.environ, "INSTANCE_NAME": name}
    probe = "from app.logging_utils import INSTANCE_ID; print(INSTANCE_ID)"
    instance = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    expected = prefix or f"{socket.gethostname()}-"
    assert instance.startswith(expected), instance
    assert len(instance) == len(expected) + 6  # the suffix still tells processes on one host apart


def test_framework_errors_use_the_error_envelope(client) -> None:
    unknown = client.get("/no/such/route")
    assert unknown.status_code == 404
    assert unknown.json()["error_code"] == "not_found"
    assert unknown.json()["request_id"] == unknown.headers["X-Request-ID"]
    wrong_method = client.delete("/health")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error_code"] == "method_not_allowed"


def test_unmatched_route_metric_label(client) -> None:
    probe = "/no/such/path/7b1e5c"
    assert client.get(probe).status_code == 404
    metrics = client.get("/metrics").text
    assert probe not in metrics  # scanners must not create one time series per path
    assert 'route="unmatched"' in metrics
